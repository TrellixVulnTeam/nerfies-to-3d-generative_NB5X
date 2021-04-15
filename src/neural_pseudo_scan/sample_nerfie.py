import functools
import logging
import os
from pathlib import Path

import click
import flax
import gin
import jax
import matplotlib.pyplot as plt
import numpy as np
import open3d as o3d
import torch
from jax import numpy as jnp
from nerfies import (
    configs,
    datasets,
    evaluation,
    image_utils,
    model_utils,
    models,
    schedules,
    training,
    utils,
    visualization,
)
from PIL import Image
from pytorch3d.structures import Pointclouds


@click.group()
def sample_nerfie():
    pass


@click.command()
@click.option("--data-dir", type=str, required=True)
@click.option("--frame-step", type=int, default=1)
@click.option("--output-dir", type=str, required=True)
@click.option("--point-cloud-filename", type=str, required=True)
@click.option("--train-dir", type=str, required=True)
def sample_points(data_dir, frame_step, output_dir, point_cloud_filename, train_dir):
    os.makedirs(output_dir, exist_ok=True)

    checkpoint_dir = Path(train_dir, "checkpoints")
    checkpoint_dir.mkdir(exist_ok=True, parents=True)

    config_path = Path(train_dir, "config.gin")
    with open(config_path, "r") as f:
        logging.info("Loading config from %s", config_path)
        config_str = f.read()
    gin.parse_config(config_str)

    config_path = Path(train_dir, "config.gin")
    with open(config_path, "w") as f:
        logging.info("Saving config to %s", config_path)
        f.write(config_str)

    exp_config = configs.ExperimentConfig()
    model_config = configs.ModelConfig()
    train_config = configs.TrainConfig()
    eval_config = configs.EvalConfig()

    datasource = datasets.from_config(
        exp_config.datasource_spec,
        image_scale=exp_config.image_scale,
        use_appearance_id=model_config.use_appearance_metadata,
        use_camera_id=model_config.use_camera_metadata,
        use_warp_id=model_config.use_warp,
        random_seed=exp_config.random_seed,
    )

    rng = jax.random.PRNGKey(exp_config.random_seed)
    np.random.seed(exp_config.random_seed + jax.host_id())
    devices = jax.devices()

    learning_rate_sched = schedules.from_config(train_config.lr_schedule)
    warp_alpha_sched = schedules.from_config(train_config.warp_alpha_schedule)
    elastic_loss_weight_sched = schedules.from_config(
        train_config.elastic_loss_weight_schedule
    )

    rng, key = jax.random.split(rng)
    params = {}
    model, params["model"] = models.nerf(
        key,
        model_config,
        batch_size=train_config.batch_size,
        num_appearance_embeddings=len(datasource.appearance_ids),
        num_camera_embeddings=len(datasource.camera_ids),
        num_warp_embeddings=len(datasource.warp_ids),
        near=datasource.near,
        far=datasource.far,
        use_warp_jacobian=train_config.use_elastic_loss,
        use_weights=train_config.use_elastic_loss,
    )

    optimizer_def = flax.optim.Adam(learning_rate_sched(0))
    optimizer = optimizer_def.create(params)
    state = model_utils.TrainState(optimizer=optimizer, warp_alpha=warp_alpha_sched(0))
    scalar_params = training.ScalarParams(
        learning_rate=learning_rate_sched(0),
        elastic_loss_weight=elastic_loss_weight_sched(0),
        background_loss_weight=train_config.background_loss_weight,
    )
    logging.info("Restoring checkpoint from %s", checkpoint_dir)
    state = flax.training.checkpoints.restore_checkpoint(checkpoint_dir, state)
    step = state.optimizer.state.step + 1
    state = flax.jax_utils.replicate(state, devices=devices)
    del params

    devices = jax.devices()

    def _model_fn(key_0, key_1, params, rays_dict, alpha):
        out = model.apply(
            {"params": params},
            rays_dict,
            warp_alpha=alpha,
            rngs={"coarse": key_0, "fine": key_1},
            mutable=False,
        )
        return jax.lax.all_gather(out, axis_name="batch")

    pmodel_fn = jax.pmap(
        # Note rng_keys are useless in eval mode since there's no randomness.
        _model_fn,
        # key0, key1, params, rays_dict, alpha
        in_axes=(0, 0, 0, 0, 0),
        devices=devices,
        donate_argnums=(3,),  # Donate the 'rays' argument.
        axis_name="batch",
    )

    render_fn = functools.partial(
        evaluation.render_image,
        model_fn=pmodel_fn,
        device_count=len(devices),
        chunk=eval_config.chunk // 2,
    )

    test_camera_paths = datasource.glob_cameras(
        Path(data_dir, "camera-paths/orbit-mild")
    )
    test_cameras = utils.parallel_map(
        datasource.load_camera, test_camera_paths, show_pbar=True
    )

    rng = rng + jax.host_id()  # Make random seed separate across hosts.
    keys = jax.random.split(rng, len(devices))

    results = []
    point_cloud_xyz = []
    point_cloud_rgb = []
    for i in range(0, len(test_cameras), frame_step):
        print(f"Rendering frame {i+1}/{len(test_cameras)}")
        camera = test_cameras[i]
        batch = datasets.camera_to_rays(camera)
        batch["metadata"] = {
            "appearance": jnp.zeros_like(
                batch["origins"][..., 0, jnp.newaxis], jnp.uint32
            ),
            "warp": jnp.zeros_like(batch["origins"][..., 0, jnp.newaxis], jnp.uint32),
        }

        (
            pred_color,
            pred_depth,
            pred_depth_med,
            pred_acc,
            sampled_points,
            weights,
        ) = render_fn(state, batch, rng=rng)

        opaqueness_mask = model_utils.compute_opaqueness_mask(
            weights, depth_threshold=0.5
        )
        points_mid = jnp.sum(opaqueness_mask[..., None] * sampled_points, axis=-2)

        point_cloud_xyz.append(np.array(points_mid.reshape(-1, 3)))
        point_cloud_rgb.append(np.array(pred_color.reshape(-1, 3)))

        results.append((pred_color, pred_depth))
        pred_depth_viz = visualization.colorize(
            pred_depth.squeeze(), cmin=datasource.near, cmax=datasource.far, invert=True
        )
        pred_color = image_utils.image_to_uint8(np.array(pred_color))
        pred_color = Image.fromarray(pred_color)
        pred_color.save(os.path.join(output_dir, f"{i:04d}.jpg"))

    with open(os.path.join(output_dir, point_cloud_filename), "wb") as f:
        np.save(
            f,
            {
                "verts": np.concatenate(point_cloud_xyz, axis=0),
                "rgb": np.concatenate(point_cloud_rgb, axis=0),
            },
        )


@click.command()
@click.option("--point-cloud-path", type=str, required=True)
def visualize_point_cloud(point_cloud_path):
    if torch.cuda.is_available():
        device = torch.device("cuda:0")
        torch.cuda.set_device(device)
    else:
        device = torch.device("cpu")

    point_cloud_np = np.load(point_cloud_path, allow_pickle=True).item()

    point_cloud = o3d.geometry.PointCloud()
    point_cloud.points = o3d.utility.Vector3dVector(point_cloud_np["verts"])
    point_cloud.colors = o3d.utility.Vector3dVector(point_cloud_np["rgb"])
    o3d.visualization.draw_geometries([point_cloud])

    point_cloud_path_o3d = os.path.splitext(point_cloud_path)[0]
    point_cloud_path_o3d = f"{point_cloud_path_o3d}_o3d.pcd"
    o3d.io.write_point_cloud(point_cloud_path_o3d, point_cloud)


sample_nerfie.add_command(sample_points)
sample_nerfie.add_command(visualize_point_cloud)


if __name__ == "__main__":
    sample_nerfie()