from typing import Any, Dict, Tuple

import torch
from lightning import LightningModule
from torchmetrics import MinMetric, MeanMetric
import logging


class RootMeanMetric(MeanMetric):
    def compute(self):
        return super().compute().sqrt()


class Wang2022LightningModule(LightningModule):
    def __init__(
        self,
        net: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler,
        compile: bool,
        autoregressive_train: bool = True,
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
        self.criterion = torch.nn.MSELoss()

        # metric objects for calculating and averaging accuracy across batches
        self.train_rmse = RootMeanMetric()
        self.val_rmse = RootMeanMetric()
        self.test_rmse = RootMeanMetric()

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
        self.val_rmse_best = MinMetric()
        self.autoregressive_train = autoregressive_train

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Perform a forward pass through the model `self.net`.

        :param x: A tensor of images.
        :return: A tensor of logits.
        """
        return self.net(x)

    def on_train_start(self) -> None:
        """Lightning hook that is called when training begins."""
        # by default lightning executes validation step sanity checks before training starts,
        # so it's worth to make sure validation metrics don't store results from these checks
        self.val_loss.reset()
        self.val_rmse.reset()
        self.val_rmse_best.reset()

    def model_step(
        self, batch: Tuple[torch.Tensor, torch.Tensor], autoregressive: bool = True
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Perform a single model step on a batch of data.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target labels.
        """
        xx, yy = batch
        mse = torch.tensor(0.0, device=self.device)
        for y in yy.transpose(0, 1):
            im = self.forward(xx)
            cur_mse = self.criterion(im, y)
            mse += cur_mse
            # generalized sliding window, works for n input frames and m output frames
            if autoregressive:
                xx = torch.cat([xx[:, im.shape[1] :], im], 1)
            else:
                xx = torch.cat([xx[:, im.shape[1] :], y], 1)

        if self.with_weight_constraint:
            weight_constraint = self.criterion(
                self.net.get_weight_constraint(), torch.tensor(0).float().to(self.device)
            )
            loss = mse + weight_constraint
        else:
            weight_constraint = None
            loss = mse

        return mse, weight_constraint, loss, yy

    def training_step(
        self, batch: Tuple[torch.Tensor, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """Perform a single training step on a batch of data from the training set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        :return: A tensor of losses between model predictions and targets.
        """
        mse, weight_constraint, loss, targets = self.model_step(
            batch, autoregressive=self.autoregressive_train
        )

        if weight_constraint is not None:
            self.train_weight_constraint(weight_constraint)
            self.log(
                "train/weight_constraint",
                self.train_weight_constraint,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
            )
        if hasattr(self.net, "alpha"):
            self.log("alpha", self.net.alpha)
        # update and log metrics
        self.train_loss(loss)
        self.train_rmse(mse.item() / targets.shape[1])
        self.log(
            "train/loss", self.train_loss, on_step=False, on_epoch=True, prog_bar=True
        )
        self.log(
            "train/rmse", self.train_rmse, on_step=False, on_epoch=True, prog_bar=True
        )

        # return loss or backpropagation will fail
        return loss

    def on_train_epoch_end(self) -> None:
        "Lightning hook that is called when a training epoch ends."
        pass

    def validation_step(
        self, batch: Tuple[torch.Tensor, torch.Tensor], batch_idx: int
    ) -> None:
        """Perform a single validation step on a batch of data from the validation set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        """
        mse, weight_constraint, loss, targets = self.model_step(batch)
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
        self.val_rmse(mse.item() / targets.shape[1])
        self.log("val/loss", self.val_loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log("val/rmse", self.val_rmse, on_step=False, on_epoch=True, prog_bar=True)

    def on_validation_epoch_end(self) -> None:
        "Lightning hook that is called when a validation epoch ends."
        acc = self.val_rmse.compute()  # get current val acc
        self.val_rmse_best(acc)  # update best so far val acc
        # log `val_acc_best` as a value through `.compute()` method, instead of as a metric object
        # otherwise metric would be reset by lightning after each epoch
        self.log(
            "val/rmse_best", self.val_rmse_best.compute(), sync_dist=True, prog_bar=True
        )

    def test_step(
        self, batch: Tuple[torch.Tensor, torch.Tensor], batch_idx: int
    ) -> None:
        """Perform a single test step on a batch of data from the test set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        """
        mse, weight_constraint, loss, targets = self.model_step(batch)
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
        self.test_rmse(mse.item() / targets.shape[1])
        self.log(
            "test/loss", self.test_loss, on_step=False, on_epoch=True, prog_bar=True
        )
        self.log(
            "test/rmse", self.test_rmse, on_step=False, on_epoch=True, prog_bar=True
        )

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
    _ = Wang2022LightningModule(None, None, None, None, None)
