import io
import json
import pprint
from collections import OrderedDict
from dataclasses import asdict, dataclass, is_dataclass
from os import path
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type, Union

import torch
import wandb
from simple_parsing import ArgumentParser, subparsers
from utils.json_utils import JsonSerializable
from torch import nn

from config import Config
from experiment import Experiment
from utils.utils import is_nonempty_dir

def launch(experiment: Experiment):
    """ Launches the experiment.
    
    TODO: Clean this up. It isn't clear exactly where the separation is
    between the Experiment.run() method and this one.
    """
    if experiment.config.verbose:     
        print("Experiment:")
        pprint.pprint(asdict(experiment), indent=1)
        print("=" * 40)

    config: Config = experiment.config
    # pprint.pprint(config_dict, indent=1)

    config_dict = experiment.to_dict()
    config.run_group = config.run_group or type(experiment).__name__

    if experiment.config.use_wandb:
        wandb_path = experiment.config.log_dir_root.joinpath('wandb')
        wandb_path.mkdir(parents=True, mode=0o777, exist_ok=True)
        
        print(f"Using wandb. Experiment name: {config.run_name}")
        if config.run_name is None:
            # TODO: Create a run name using the coefficients of the tasks, etc?
            # At the moment, if no run name is given, ths
            pass
        run = config.wandb_init(experiment)
        wandb.run.save()
        
        print(f"Using wandb. Group name: {config.run_group} run name: {config.run_name}, log_dir: {config.log_dir}")
    
    # if experiment.done:
    #     print(f"Experiment is already done. Exiting.")
    #     exit(0)
    if experiment.started:
        print(f"Experiment is incomplete at directory {config.log_dir}.")
        # TODO: pick up where we left off ?
        # latest_checkpoint = log_dir / "checkpoints" / "todo"
        # settings.experiment = torch.load(latest_checkpoints)
    
    try:
        print("-" * 10, f"Starting experiment '{type(experiment).__name__}' ({config.log_dir})", "-" * 10)
        
        experiment.run()
        
        print("-" * 10, f"Experiment '{type(experiment).__name__}' is done.", "-" * 10)
        experiment.cleanup()
    
    except Exception as e:
        print(f"Experiment crashed: {e}")
        raise e
    

def main(argv: Optional[List[str]]=None):
    import textwrap
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(description=textwrap.dedent("""\
        Which Experiment or script to run. Experiments can also be launched by
        executing the corresponding script directly. To get a more detailed view
        of the parameters of each experiment, use the corresponding "--help"
        option, as in "python main.py task-incremental --help"."""))
    # Add a subparser for each Experiment type:
    from iid import IID
    subparser = subparsers.add_parser("iid", help=IID.__doc__)
    subparser.add_arguments(IID, "experiment")
    
    from task_incremental import TaskIncremental
    subparser = subparsers.add_parser("task-incremental", help=TaskIncremental.__doc__)
    subparser.add_arguments(TaskIncremental, "experiment")

    from task_incremental_sem_sup import TaskIncremental_Semi_Supervised
    subparser = subparsers.add_parser("task-incremental-semi-sup", help=TaskIncremental_Semi_Supervised.__doc__)
    subparser.add_arguments(TaskIncremental_Semi_Supervised, "experiment")

    from active_remembering import ActiveRemembering
    subparser = subparsers.add_parser("active-remembering", help=ActiveRemembering.__doc__)
    subparser.add_arguments(ActiveRemembering, "experiment")

    # Scripts to execute:
    from scripts.make_oml_plot import OmlFigureOptions
    
    subparser = subparsers.add_parser("make_oml_plot", help=OmlFigureOptions.__doc__)
    subparser.add_arguments(OmlFigureOptions, "options")  # Same here.
    
    args = parser.parse_args(argv)

    experiment: Experiment = args.experiment
    launch(experiment)


if __name__ == "__main__":
    main()
