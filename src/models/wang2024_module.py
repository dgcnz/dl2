from typing import Any, Dict, Tuple

import torch
from lightning import LightningModule
from torchmetrics import MinMetric, MeanMetric
import logging


class Wang2024LightningModule(LightningModule):
    def __init__(
        self,
        net: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler,
        compile: bool,
    ) -> None:
        """Initialize a `pl_classifier`.

        :param net: The model to train.
        :param optimizer: The optimizer to use for training.
        :param scheduler: The learning rate scheduler to use for training.
        """

        super().__init__()

        # this line allows to access init params with 'self.hparams' attribute
        # also ensures init params will be stored in ckpt
        self.save_hyperparameters(logger=False)

        self.cli_logger = logging.getLogger(self.__class__.__name__)
        self.net = net

        # loss function
        self.criterion = torch.nn.L1Loss()

        # metric objects for calculating and averaging accuracy across batches
        self.train_mae = MeanMetric()
        self.val_mae = MeanMetric()
        self.test_mae = MeanMetric()

        # for averaging loss across batches
        self.train_loss = MeanMetric()
        self.val_loss = MeanMetric()
        self.test_loss = MeanMetric()

        # for tracking weight constraint
        self.with_weight_constraint = hasattr(self.net, "get_weight_constraint")
        if self.with_weight_constraint:
            self.with_weight_constraint = True
            self.train_weight_constraint = MeanMetric()
            self.val_weight_constraint = MeanMetric()
            self.test_weight_constraint = MeanMetric()
            self.cli_logger.info(
                f"Network {self.net.__class__.__name__} has weight constraint"
            )
        else:
            self.cli_logger.info(
                f"Network {self.net.__class__.__name__} does not have weight constraint"
            )

        # for tracking best so far validation accuracy
        self.val_mae_best = MinMetric()

    def forward(self, lrs: torch.Tensor) -> torch.Tensor:
        """Perform a forward pass through the model `self.net`."""
        lrs = lrs.flatten(1, 2)  # flatten the time and channel dimensions
        return self.net(lrs)

    def on_train_start(self) -> None:
        """Lightning hook that is called when training begins."""
        # by default lightning executes validation step sanity checks before training starts,
        # so it's worth to make sure validation metrics don't store results from these checks
        self.val_loss.reset()
        self.val_mae.reset()
        self.val_mae_best.reset()

    def model_step(
        self, batch: dict[str, torch.Tensor]
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Perform a single model step on a batch of data."""
        lrs, hr = batch["lrs"], batch["hr"]
        out = self.forward(lrs)
        mae = self.criterion(out, hr)

        if self.with_weight_constraint:
            weight_constraint = self.criterion(
                self.net.get_weight_constraint(), torch.tensor(0).float().cuda()
            )
            loss = mae + weight_constraint
        else:
            weight_constraint = None
            loss = mae

        return mae, weight_constraint, loss

    def training_step(
        self, batch: dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """Perform a single training step on a batch of data from the training set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        :return: A tensor of losses between model predictions and targets.
        """
        mae, weight_constraint, loss = self.model_step(batch)

        if weight_constraint is not None:
            self.train_weight_constraint(weight_constraint)
            self.log(
                "train/weight_constraint",
                self.train_weight_constraint,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
            )

        # update and log metrics
        self.train_loss(loss)
        self.train_mae(mae.item())
        self.log(
            "train/loss", self.train_loss, on_step=False, on_epoch=True, prog_bar=True
        )
        self.log(
            "train/mae", self.train_mae, on_step=False, on_epoch=True, prog_bar=True
        )

        # return loss or backpropagation will fail
        return loss

    def validation_step(self, batch: dict[str, torch.Tensor], batch_idx: int) -> None:
        """Perform a single validation step on a batch of data from the validation set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        """
        mae, weight_constraint, loss = self.model_step(batch)
        if weight_constraint is not None:
            self.val_weight_constraint(weight_constraint)
            self.log(
                "val/weight_constraint",
                self.val_weight_constraint,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
            )

        # update and log metrics
        self.val_loss(loss)
        self.val_mae(mae.item())
        self.log("val/loss", self.val_loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log("val/mae", self.val_mae, on_step=False, on_epoch=True, prog_bar=True)

    def on_validation_epoch_end(self) -> None:
        "Lightning hook that is called when a validation epoch ends."
        acc = self.val_mae.compute()  # get current val acc
        self.val_mae_best(acc)  # update best so far val acc
        # log `val_acc_best` as a value through `.compute()` method, instead of as a metric object
        # otherwise metric would be reset by lightning after each epoch
        self.log(
            "val/mae_best", self.val_mae_best.compute(), sync_dist=True, prog_bar=True
        )

    def test_step(self, batch: dict[str, torch.Tensor], batch_idx: int) -> None:
        """Perform a single test step on a batch of data from the test set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        """
        mae, weight_constraint, loss = self.model_step(batch)
        if weight_constraint is not None:
            self.test_weight_constraint(weight_constraint)
            self.log(
                "test/weight_constraint",
                self.test_weight_constraint,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
            )

        # update and log metrics
        self.test_loss(loss)
        self.test_mae(mae.item())
        self.log(
            "test/loss", self.test_loss, on_step=False, on_epoch=True, prog_bar=True
        )
        self.log("test/mae", self.test_mae, on_step=False, on_epoch=True, prog_bar=True)

    def on_test_epoch_end(self) -> None:
        """Lightning hook that is called when a test epoch ends."""
        pass

    def setup(self, stage: str) -> None:
        """Lightning hook that is called at the beginning of fit (train + validate), validate,
        test, or predict.

        This is a good hook when you need to build models dynamically or adjust something about
        them. This hook is called on every process when using DDP.

        :param stage: Either `"fit"`, `"validate"`, `"test"`, or `"predict"`.
        """
        if self.hparams.compile and stage == "fit":
            self.net = torch.compile(self.net)

    def configure_optimizers(self) -> Dict[str, Any]:
        """Choose what optimizers and learning-rate schedulers to use in your optimization.
        Normally you'd need one. But in the case of GANs or similar you might have multiple.

        Examples:
            https://lightning.ai/docs/pytorch/latest/common/lightning_module.html#configure-optimizers

        :return: A dict containing the configured optimizers and learning-rate schedulers to be used for training.
        """
        optimizer = self.hparams.optimizer(params=self.trainer.model.parameters())
        if self.hparams.scheduler is not None:
            scheduler = self.hparams.scheduler(optimizer=optimizer)
            return {
                "optimizer": optimizer,
                "lr_scheduler": {
                    "scheduler": scheduler,
                    "monitor": "val/loss",
                    "interval": "epoch",
                    "frequency": 1,
                },
            }
        return {"optimizer": optimizer}


if __name__ == "__main__":
    _ = Wang2024LightningModule(None, None, None, None)
