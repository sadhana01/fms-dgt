# Standard
from typing import Dict, List, Optional
import json
import os

# Local
from fms_dgt.base.databuilder import DataBuilder
from fms_dgt.base.registry import get_data_builder
from fms_dgt.base.task_card import TaskRunCard
from fms_dgt.index import DataBuilderIndex
import fms_dgt.utils as utils

sdg_logger = utils.sdg_logger


def generate_data(
    task_kwargs: Dict,
    builder_kwargs: Dict,
    data_paths: Optional[List[str]] = None,
    config_path: Optional[str] = None,
    include_builder_paths: Optional[List[str]] = None,
    build_id: Optional[str] = None,
):
    """Generate data for a set of tasks using their respective data builders

    Args:
        task_kwargs (Dict): A dictionary of keyword arguments to pass to each task.
        builder_kwargs (Dict): A dictionary of keyword arguments to pass to each data builder.
        data_paths (Optional[List[str]], optional): A list of paths to data files.
        config_path (Optional[str], optional): A path to a configuration file.
        include_builder_paths (Optional[List[str]], optional): A list of paths to search for data builders.
    """
    data_paths = data_paths or []
    builder_overrides = None
    task_overrides = dict()

    if config_path:
        addlt_data_paths, builder_overrides, task_overrides = utils.load_joint_config(
            config_path
        )
        data_paths.extend(addlt_data_paths)

    if not data_paths and not config_path:
        raise ValueError(
            f"One of ['data-paths', 'config-path'] must be provided in the arguments"
        )

    data_paths = list(set(data_paths))

    # check data_path first then seed_tasks_path
    # throw an error if both not found
    # pylint: disable=broad-exception-caught,raise-missing-from
    task_inits = []
    for data_path in data_paths:
        if data_path and os.path.exists(data_path):
            for task_init in utils.read_data(data_path):
                task_init = {
                    **task_overrides.get(task_init["task_name"], dict()),
                    **task_init,
                }
                task_inits.append(task_init)
        else:
            raise FileExistsError(f"Error: data path ({data_path}) does not exist.")

    # gather data builders here
    builder_list = [t["data_builder"] for t in task_inits]
    builder_index = DataBuilderIndex(
        include_builder_paths=include_builder_paths,
    )
    builder_names = builder_index.match_builders(builder_list)
    sdg_logger.debug("All builders: %s", builder_names)
    builder_missing = set(
        [
            builder
            for builder in builder_list
            if builder not in builder_names and "*" not in builder
        ]
    )

    if builder_missing:
        missing = ", ".join(builder_missing)
        raise ValueError(f"Builder specifications not found: [{missing}]")

    for builder_name, builder_cfg in builder_index.load_builder_configs(
        builder_names, config_overrides=builder_overrides
    ).items():

        # we batch together tasks at the level of data builders
        builder_info = builder_index.builder_index[builder_name]
        builder_dir = builder_info.get("builder_dir")
        if isinstance(builder_cfg, tuple):
            _, builder_cfg = builder_cfg
            if builder_cfg is None:
                continue

        sdg_logger.debug("Builder config for %s: %s", builder_name, builder_cfg)

        all_builder_kwargs = {
            "config": builder_cfg,
            "task_kwargs": [
                {
                    # get task card
                    "task_card": TaskRunCard(
                        task_name=task_init.get("task_name"),
                        databuilder_name=task_init.get("data_builder"),
                        task_spec=json.dumps({**task_init, **task_kwargs}),
                        databuilder_spec=json.dumps(
                            utils.load_nested_paths(builder_cfg, builder_dir)
                        ),
                        build_id=build_id,
                    ),
                    # other params
                    **{**task_init, **task_kwargs},
                }
                for task_init in task_inits
                if task_init["data_builder"] == builder_name
            ],
            **builder_kwargs,
        }

        try:
            # first see if databuilder is loaded by default
            data_builder: DataBuilder = get_data_builder(
                builder_name, **all_builder_kwargs
            )
        except KeyError as e:
            if f"Attempted to load data builder '{builder_name}'" in str(e):
                utils.import_builder(builder_dir)
                data_builder: DataBuilder = get_data_builder(
                    builder_name, **all_builder_kwargs
                )
            else:
                raise e

        # TODO: ship this off
        data_builder.execute_tasks()

        # TODO: cleanup
        del data_builder
