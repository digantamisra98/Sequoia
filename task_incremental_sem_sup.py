
import tqdm
import torch
from sys import getsizeof
from torch import Tensor, nn
from itertools import repeat, cycle
from models.classifier import Classifier
from task_incremental import TaskIncremental
from dataclasses import dataclass
from torch.utils.data import Subset
from datasets.subset import VisionDatasetSubset
from common.losses import LossInfo
from datasets.ss_dataset import get_semi_sampler
from addons.ewc import EWC_wrapper
from addons.curvature_analyser import Analyser
from collections import OrderedDict, defaultdict
from itertools import accumulate
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.data import DataLoader, Dataset, TensorDataset
from typing import Dict, Iterable, List, Tuple, Union, Optional, Any
from typing import (Any, ClassVar, Dict, Generator, Iterable, List, Optional, Tuple, Type, Union)
from common.losses import LossInfo, TrainValidLosses
from simple_parsing import mutable_field, list_field
from common.task import Task

@dataclass
class TaskIncremental_Semi_Supervised(TaskIncremental):
    """ Evaluates the model in the same setting as the OML paper's Figure 3.
    """
    unsupervised_epochs_per_task: int = 0
    supervised_epochs_per_task: int = 10
    # Coefficient of the EWC regularizer. Higher lamda -> more penalty for
    # changing the parameters between tasks.
    ewc_lamda: float = 10.

    # Ratio of samples that have a corresponding label.
    ratio_labelled: float = 0.2

    def __post_init__(self):
        super().__post_init__()
        self.train_samplers_labelled: List[SubsetRandomSampler] = []
        self.train_samplers_unlabelled: List[SubsetRandomSampler] = []
        self.valid_samplers_labelled: List[SubsetRandomSampler] = []
        self.valid_samplers_unlabelled: List[SubsetRandomSampler] = []

        self.epoch: Optional[int] = None
        self.epoch_length: Optional[int] = None
        self.batch_idx: Optional[int] = None

    def init_model(self) -> Classifier:
        self.logger.debug("init model")
        model = super().init_model()
        #model = Analyser(model)
        if self.ewc_lamda>0:
            self.logger.info(f"Using EWC with a lambda of {self.ewc_lamda}")
            #TODO: n_ways should be self.n_classes_per_task, but model outputs 10 way classifier instead of self.n_classes_per_task - way
            model = EWC_wrapper(model, lamda=self.ewc_lamda, n_ways=self.n_classes_per_task, device=self.config.device)

        return model

    def load_datasets(self, tasks: List[Task]) -> None:
        """Create the train, valid and cumulative datasets for each task.

        Returns:
            List[List[int]]: The groups of classes for each task.
        """
        # download the dataset.
        self.train_dataset, self.valid_dataset = self.dataset.load(data_dir=self.config.data_dir)

        # safeguard the entire training dataset.
        train_full_dataset = self.train_dataset
        valid_full_dataset = self.valid_dataset

        self.train_datasets.clear()
        self.valid_datasets.clear()

        for i, task in enumerate(tasks):
            train = VisionDatasetSubset(train_full_dataset, task)
            valid = VisionDatasetSubset(valid_full_dataset, task)
            sampler_train, sampler_train_unlabelled = get_semi_sampler(train.targets,p=self.ratio_labelled)
            sampler_valid, sampler_valid_unlabelled = get_semi_sampler(valid.targets, p=1.)

            # self.train_datasets.append((train, sampler_train, sampler_train_unlabelled))
            # self.valid_datasets.append((valid, sampler_valid, sampler_valid_unlabelled))
            self.train_datasets.append(train)
            self.train_samplers_labelled.append(sampler_train)
            self.train_samplers_unlabelled.append(sampler_train_unlabelled)

            self.valid_datasets.append(valid)
            self.valid_samplers_labelled.append(sampler_valid)
            self.valid_samplers_unlabelled.append(sampler_valid_unlabelled)

        # Use itertools.accumulate to do the summation of validation datasets.
        self.valid_cumul_datasets = list(accumulate(self.valid_datasets))

        for i, (train, valid, cumul) in enumerate(zip(self.train_datasets,
                                                      self.valid_datasets,
                                                      self.valid_cumul_datasets)):
            self.save_images(i, train, prefix="train_")
            self.save_images(i, valid, prefix="valid_")
            self.save_images(i, cumul, prefix="valid_cumul_")

    def get_dataloaders(self,
                       dataset: Dataset,
                       sampler_labelled: SubsetRandomSampler,
                       sampler_unlabelled: SubsetRandomSampler) -> Tuple[DataLoader,DataLoader]:
        loader_train_labelled = super().get_dataloader(dataset, sampler=sampler_labelled)
        loader_train_unlabelled = super().get_dataloader(dataset, sampler=sampler_unlabelled)
        return (loader_train_labelled, loader_train_unlabelled)


    def run(self):
        """Evaluates a model/method in the classical "task-incremental" setting.

        NOTE: We evaluate the performance on all tasks:
        - When the task has NOT been trained on before, we evaluate the ability
        of the representations to "generalize" to unseen tasks by training a KNN
        classifier on the representations of target task's training set, and
        evaluating it on the representations of the target task's validation set.
        - When the task has been previously trained on, we evaluate the
        classification loss/metrics (and auxiliary tasks, if any) as well as the
        representations with the KNN classifier.

        Roughly equivalent to the following pseudocode:
        ```
        # Training and Validdation datasets
        train_datasets: Dataset[n_tasks]
        valid_datasets: Dataset[n_tasks]

        # Arrays containing the loss/performance metrics. (Value at idx (i, j)
        # is the preformance on task j after having trained on tasks [0:i].)

        knn_losses: LossInfo[n_tasks][n_tasks]
        tasks_losses: LossInfo[n_tasks][j]  #(this is a lower triangular matrix)

        # Array of objects containing the loss/performance on tasks seen so far.
        cumul_losses: LossInfo[n_tasks]

        for i in range(n_tasks):
            train_until_convergence(train_datasets[i], valid_datasets[i])

            # Cumulative (supervised) validation performance.
            cumul_loss = LossInfo()

            for j in range(n_tasks):
                # Evaluate the representations with a KNN classifier.
                knn_loss_j = evaluate_knn(train_dataset[j], valid_datasets[j])
                knn_losses[i][j] = knn_loss_j

                if j <= i:
                    # We have previously trained on this class.
                    loss_j = evaluate(valid_datasets[j])
                    task_losses[i][j] = loss_j
                    cumul_loss += loss_j

            cumul_losses[i] = cumul_loss
        ```
        """
        self.model = self.init_model()
        self.log_dir.mkdir(parents=True, exist_ok=True)

        #if (self.started or self.restore_from_path) and not self.config.debug:
        #    self.logger.info(f"Experiment was already started in the past.")
        #    self.restore_from_path = self.checkpoints_dir / "state.json"
        #    self.logger.info(f"Will load state from {self.restore_from_path}")
        #    self.load_state(self.restore_from_path)

        if self.done:
            self.logger.info(f"Experiment is already done.")
            # exit()

        if self.state.global_step == 0:
            self.logger.info("Starting from scratch!")
            self.state.tasks = self.create_tasks_for_dataset(self.dataset)
        else:
            self.logger.info(f"Starting from global step {self.state.global_step}")
            self.logger.info(f"i={self.state.i}, j={self.state.j}")

        self.tasks = self.state.tasks
        #if not self.config.debug:
        #    self.save()

        # Load the datasets
        self.load_datasets(self.tasks)
        self.n_tasks = len(self.tasks)

        self.logger.info(f"Class Ordering: {self.state.tasks}")

        if self.state.global_step == 0:
            self.state.knn_losses = [[None] * self.n_tasks] * self.n_tasks  # [N,N]
            self.state.task_losses = [[None] * (i + 1) for i in range(self.n_tasks)]  # [N,J]
            self.state.cumul_losses = [None] * self.n_tasks  # [N]

        for i in range(self.state.i, self.n_tasks):
            self.state.i = i
            self.logger.info(f"Starting task {i} with classes {self.tasks[i]}")



            # If we are using a multihead model, we give it the task label (so
            # that it can spawn / reuse the output head for the given task).
            if self.multihead:
                self.on_task_switch(self.tasks[i])

            #if i > 0:
            #    fisher_norm, sum = self.model.analyse_curvature(i - 1, self.multihead, train_dataloader_labelled, 100)
            #    self.log({'Curvature/fisher_norm': fisher_norm, 'Curvature/sum_eigenvalues': sum})

            # Training and validation datasets for task i.
            # train_i, sampler_train_i, sampler_unlabelled_i = self.train_datasets[i]
            # valid_i, sampler_valid_i, sampler_valid_unlabelled_i = self.valid_datasets[i]

            train_i = self.train_datasets[i]
            train_sampler_labeled_i = self.train_samplers_labelled[i]
            train_sampler_unlabelled_i = self.train_samplers_unlabelled[i]

            valid_i = self.valid_datasets[i]
            valid_sampler_labelled_i = self.valid_samplers_labelled[i]
            valid_sampler_unlabelled_i = self.valid_samplers_unlabelled[i]

            # EWC_specific: pass EWC_rapper the loader to compute fisher
            # call befor task change
            # ====================
            if self.ewc_lamda > 0:
                self.model.current_task_loader = self.get_dataloaders(
                    dataset=train_i,
                    sampler_labelled=train_sampler_labeled_i,
                    sampler_unlabelled=train_sampler_unlabelled_i)[0]
            # ====================


            with self.plot_region_name(f"Learn Task {i}"):
                # We only train (unsupervised) if there is at least one enabled
                # auxiliary task and if the maximum number of unsupervised
                # epochs per task is greater than zero.
                self_supervision_on = any(task.enabled for task in self.model.tasks.values())

                if self_supervision_on and self.unsupervised_epochs_per_task:
                    # Temporarily remove the labels.
                    with train_i.without_labels(), valid_i.without_labels():
                        # Un/self-supervised training on task i.
                        self.state.all_losses += self.train_until_convergence(
                            (train_i, train_sampler_labeled_i, train_sampler_unlabelled_i),
                            (valid_i, valid_sampler_labelled_i, valid_sampler_unlabelled_i),
                            max_epochs=self.unsupervised_epochs_per_task,
                            description=f"Task {i} (Unsupervised)", patience=10)

                # Train (supervised) on task i.
                self.state.all_losses += self.train_until_convergence(
                    (train_i, train_sampler_labeled_i, train_sampler_unlabelled_i),
                    (valid_i, valid_sampler_labelled_i, valid_sampler_unlabelled_i),
                    max_epochs=self.supervised_epochs_per_task,
                    description=f"Task {i} (Supervised)", patience=10
                )
                self.logger.debug(f"Size the state object: {getsizeof(self.state)}")

            # TODO: save the state during training.
            #self.save()
            #  Evaluate on all tasks (as described above).
            cumul_loss = LossInfo(f"cumul_losses[{i}]")
            for j in range(self.state.j, self.n_tasks):
                self.state.j = j
                train_j = self.train_datasets[j]
                train_sampler_labelled_j = self.train_samplers_labelled[j]
                train_sampler_unlabelled_j = self.train_samplers_unlabelled[j]
                
                valid_j = self.valid_datasets[j]
                valid_sampler_labelled_j = self.valid_samplers_labelled[j]
                valid_sampler_unlabelled_j = self.valid_samplers_unlabelled[j]
                
                train_dataloader_labelled, train_dataloader_unlabelled = self.get_dataloaders(
                    dataset=train_j,
                    sampler_labelled=train_sampler_labelled_j,
                    sampler_unlabelled=train_sampler_unlabelled_j,
                )
                valid_dataloader_labelled, valid_dataloader_unlablled = self.get_dataloaders(
                    dataset=valid_j,
                    sampler_labelled=valid_sampler_labelled_j,
                    sampler_unlabelled=valid_sampler_unlabelled_j,
                )
                # Measure how linearly separable the representations of task j
                # are by training and evaluating a KNNClassifier on the data of task j.
                train_knn_loss, valid_knn_loss = self.test_knn(
                    train_dataloader_labelled,
                    valid_dataloader_labelled,
                    description=f"KNN[{i}][{j}]"
                )
                self.log({
                    f"knn_losses[{i}][{j}]/train": train_knn_loss.to_log_dict(),
                    f"knn_losses[{i}][{j}]/valid": valid_knn_loss.to_log_dict(),
                })
                self.state.knn_losses[i][j] = valid_knn_loss
                accuracy = valid_knn_loss.metrics["KNN"].accuracy
                loss = valid_knn_loss.total_loss

                self.logger.info(f"knn_losses[{i}][{j}]/valid Accuracy: {accuracy:.2%}, loss: {loss}")

                if j <= i:
                    # If we have previously trained on this task:
                    self.on_task_switch(self.tasks[j])

                    loss_j = self.test(dataloader=valid_dataloader_labelled, description=f"task_losses[{i}][{j}]")
                    cumul_loss += loss_j

                    self.state.task_losses[i][j] = loss_j
                    self.log({f"task_losses[{i}][{j}]": loss_j.to_log_dict()})

                #self.save()

            self.state.cumul_losses[i] = cumul_loss
            self.state.j = 0

            valid_log_dict = cumul_loss.to_log_dict()
            self.log({f"cumul_losses[{i}]": valid_log_dict})

        # TODO: Save the results to a json file.
        #self.save(self.results_dir)
        #  Evaluate on all tasks (as described above).
        cumul_loss = LossInfo(f"cumul_losses[{i}]")

        for j in range(self.state.j, self.n_tasks):
            self.state.j = j

            train_j = self.train_datasets[j]
            train_sampler_labelled_j = self.train_samplers_labelled[j]
            train_sampler_unlabelled_j = self.train_samplers_unlabelled[j]
            
            valid_j = self.valid_datasets[j]
            valid_sampler_labelled_j = self.valid_samplers_labelled[j]
            valid_sampler_unlabelled_j = self.valid_samplers_unlabelled[j]

            train_dataloader_labelled, train_dataloader_unlabelled = self.get_dataloaders(
                dataset=train_j,
                sampler_labelled=train_sampler_labelled_j,
                sampler_unlabelled=train_sampler_unlabelled_j,
            )
            valid_dataloader_labelled, valid_dataloader_unlablled = self.get_dataloaders(
                dataset=valid_j,
                sampler_labelled=valid_sampler_labelled_j,
                sampler_unlabelled=valid_sampler_unlabelled_j,
            )
            # Measure how linearly separable the representations of task j
            # are by training and evaluating a KNNClassifier on the data of task j.

            train_knn_loss, valid_knn_loss = self.test_knn(
                train_dataloader_labelled,
                valid_dataloader_labelled,
                description=f"KNN[{i}][{j}]"
            )
            self.log({
                f"knn_losses[{i}][{j}]/train": train_knn_loss.to_log_dict(),
                f"knn_losses[{i}][{j}]/valid": valid_knn_loss.to_log_dict(),
            })
            self.state.knn_losses[i][j] = valid_knn_loss
            accuracy = valid_knn_loss.metrics["KNN"].accuracy
            loss = valid_knn_loss.total_loss

            self.logger.info(f"knn_losses[{i}][{j}]/valid Accuracy: {accuracy:.2%}, loss: {loss}")

            if j <= i:
                # If we have previously trained on this task:
                self.on_task_switch(self.tasks[j])

                loss_j = self.test(dataloader=valid_dataloader_labelled, description=f"task_losses[{i}][{j}]")
                cumul_loss += loss_j

                self.state.task_losses[i][j] = loss_j
                self.log({f"task_losses[{i}][{j}]": loss_j.to_log_dict()})

            #self.save()
        self.state.cumul_losses[i] = cumul_loss
        self.state.j = 0

        valid_log_dict = cumul_loss.to_log_dict()
        self.log({f"cumul_losses[{i}]": valid_log_dict})

        # TODO: Save the results to a json file.

        #self.save(self.results_dir)
        # TODO: save the rest of the state.

        # from utils.plotting import maximize_figure
        # Make the forward-backward transfer grid figure.
        # grid = self.make_transfer_grid_figure(self.state.knn_losses, self.state.task_losses, self.state.cumul_losses)
        # grid.savefig(self.plots_dir / "transfer_grid.png")

        # # make the plot of the losses (might not be useful, since we could also just do it in wandb).
        # fig = self.make_loss_figure(self.state.all_losses, self.plot_sections)
        # fig.savefig(self.plots_dir / "losses.png")

        # if self.config.debug:
        #     fig.show()
        #     fig.waitforbuttonpress(10)

    def train_until_convergence(self, train_dataset: Tuple[Dataset, SubsetRandomSampler, SubsetRandomSampler],
                                valid_dataset: Tuple[Dataset, SubsetRandomSampler, SubsetRandomSampler],
                                max_epochs: int,
                                description: str = None,
                                patience: int = 10) -> Tuple[Dict[int, LossInfo], Dict[int, LossInfo]]:
        train_dataloader_labelled, train_dataloader_unlabelled  = self.get_dataloaders(*train_dataset)
        valid_dataloader_labelled, valid_dataloader_unlablled  = self.get_dataloaders(*valid_dataset)
        n_steps = len(train_dataloader_unlabelled) if len(train_dataloader_unlabelled)>len(train_dataloader_labelled) else len(train_dataloader_labelled)

        if self.config.debug_steps:
            from itertools import islice
            n_steps = self.config.debug_steps
            train_dataloader = islice(train_dataloader_labelled, 0, n_steps)  # type: ignore

        train_losses: Dict[int, LossInfo] = OrderedDict()
        valid_losses: Dict[int, LossInfo] = OrderedDict()

        valid_loss_gen = self.valid_performance_generator(valid_dataloader_labelled)

        best_valid_loss: Optional[float] = None
        counter = 0

        message: Dict[str, Any] = OrderedDict()
        for epoch in range(max_epochs):
            self.epoch=epoch
            pbar = tqdm.tqdm(zip(cycle(train_dataloader_labelled),train_dataloader_unlabelled), total=n_steps)
            desc = description or ""
            desc += " " if desc and not desc.endswith(" ") else ""
            desc += f"Epoch {epoch}"
            pbar.set_description(desc + " Train")
            self.epoch_length = len(train_dataloader_unlabelled)

            for batch_idx, train_loss in enumerate(self.train_iter_semi_sup(pbar)):
                self.batch_idx = batch_idx
                if batch_idx % self.config.log_interval == 0:
                    # get loss on a batch of validation data:
                    valid_loss = next(valid_loss_gen)
                    valid_losses[self.global_step] = valid_loss
                    train_losses[self.global_step] = train_loss

                    message.update(train_loss.to_pbar_message())
                    message.update(valid_loss.to_pbar_message())
                    pbar.set_postfix(message)

                    train_log_dict = train_loss.to_log_dict()
                    valid_log_dict = valid_loss.to_log_dict()
                    self.log({"Train": train_log_dict, "Valid": valid_log_dict})

            # perform a validation epoch.
            val_desc = desc + " Valid"
            val_loss_info = self.test(valid_dataloader_labelled, description=val_desc)
            val_loss = val_loss_info.total_loss

            if best_valid_loss is None or val_loss.item() < best_valid_loss:
                counter = 0
                best_valid_loss = val_loss.item()
            else:
                counter += 1
                print(f"Validation Loss hasn't decreased over the last {counter} epochs.")
                if counter == patience:
                    print(
                        f"Exiting at step {self.global_step}, as validation loss hasn't decreased over the last {patience} epochs.")
                    break
        return train_losses, valid_losses

    def test(self, dataloader: DataLoader, description: str = None, name: str = "Test") -> LossInfo:
        pbar = tqdm.tqdm(dataloader)
        desc = (description or "Test Epoch")

        pbar.set_description(desc)
        total_loss = LossInfo(name)
        message: Dict[str, Any] = OrderedDict()

        for batch_idx, loss in enumerate(self.test_iter(pbar)):
            total_loss += loss

            if batch_idx % self.config.log_interval == 0:
                message.update(total_loss.to_pbar_message())
                pbar.set_postfix(message)

        return total_loss

    def valid_performance_generator(self, periodic_valid_dataloader: DataLoader) -> Generator[LossInfo, None, None]:
        while True:
            for batch in periodic_valid_dataloader:
                data = batch[0].to(self.model.device)
                target = batch[1].to(self.model.device) if len(batch) == 2 else None
                yield self.test_batch(data, target)

    def train_iter_semi_sup(self, dataloader: DataLoader) -> Iterable[LossInfo]:
        self.model.train()
        for batch_sup, batch_unsup in dataloader:
            data, target = self.preprocess(batch_sup)
            u, _ = self.preprocess(batch_unsup)
            yield self.train_batch_semi_sup(data,target,u)

    def train_batch_semi_sup(self, data: Tensor, target: Optional[Tensor], u: Tensor) -> LossInfo:
        self.model.optimizer.zero_grad()
        batch_loss_info = self.model.get_loss(data, target) + self.model.get_loss(u, None)

        total_loss = batch_loss_info.total_loss
        total_loss.backward()
        self.model.optimizer_step(global_step=self.global_step,
                                  epoch = self.epoch,
                                  epoch_length=self.epoch_length,
                                  update_number=self.batch_idx)
        self.global_step += data.shape[0]
        return batch_loss_info


if __name__ == "__main__":
    from simple_parsing import ArgumentParser
    parser = ArgumentParser()
    parser.add_arguments(TaskIncremental_Semi_Supervised, dest="experiment")
    
    args = parser.parse_args()
    experiment: TaskIncremental = args.experiment
    
    from main import launch
    launch(experiment)
