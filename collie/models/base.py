import os
import inspect
import importlib
from abc import abstractmethod
from typing import Union, Optional
from huggingface_hub import snapshot_download

from torch import nn
from torch import distributed as dist
from deepspeed.runtime.pipe.topology import PipeModelDataParallelTopology
from collie.module import PipelineModel, GPTLMLoss
from collie.trainer.arguments import Arguments, load_config
from collie.log import logger
from collie.utils import setup_distributation

class BaseModel(nn.Module):
    """
    Base model of CoLLiE.

    Every new model should inherit this class.
    """
    @classmethod
    def from_config(cls, args: Union[Arguments, str], **kwargs):
        """
        Load arguments from config.
        """
        if isinstance(args, str) and os.path.exists(args):
            args = Arguments.from_pretrained(args)
        args.update(**kwargs)
        setup_distributation(args)
        model_cls = cls._get_model_cls(args)
        if args.pp_size == 1:
            model = super().__new__(model_cls)
            model.__init__(args)
            dist.barrier()
            return model
        else:
            pipeline_model =  PipelineModel(
                layers=model_cls.pipeline_layers(args), base_seed=args.seed,
                partition_method=args.pp_partition_method,
                topology=PipeModelDataParallelTopology(
                    num_pp=args.pp_size,
                    num_dp=args.dp_size,
                    num_mp=args.tp_size
                ), loss_fn=GPTLMLoss()
            )
            setattr(pipeline_model, "args", args)
            return pipeline_model
            
    def __new__(cls, args: Arguments, **kwargs):
        return cls.from_config(args, **kwargs)

    @classmethod
    def from_pretrained(cls, model_path_or_name: str, args:Optional[Union[Arguments, str]] = None, **kwargs):
        """
        :param model_path_or_name: str
        :param args: str, Arguments or None. If None, we will load arguments
            from `model_path_or_name`.
        :param kwargs:
            - process_exclusion: Whether to load checkpoints one by one to 
              save memory.
            parameters to be set at Arguments.
        """
        process_exclusion = kwargs.pop("process_exclusion", False)
        if dist.is_initialized() and process_exclusion:
            logger.warning(
                "Distributed group is not initialized and `process_exclusion` "
                "will not take effect."
            )
        if not os.path.exists(model_path_or_name):
            model_path_or_name = snapshot_download(model_path_or_name)
        if args is None:
            args = model_path_or_name
        if isinstance(args, str):
            # prevent duplicate `from_pretrained`` in load_parallel
            args = Arguments.from_pretrained(args)
        model = cls.from_config(args, **kwargs)
        state_dict = cls.load_parallel_state_dict(
            path=model_path_or_name, args=args,
            process_exclusion=process_exclusion,
        )
        model.load_state_dict(state_dict)
        return model

    @classmethod
    def pipline_layers(cls, args: Union[Arguments, str]):
        """
        Get layers of pipeline.

        :return: list
        """
        raise NotImplementedError(
            "To use pipeline parallelism, you need to implement "
            "`pipeline_layers` for your model."
        )

    @staticmethod
    @abstractmethod
    def load_parallel_state_dict(path: str, args: Union[Arguments, str],
                                 process_exclusion: bool = False):
        """
        Load state_dict from ``path``.

        The format of pretrained model should be the same as that of
        `huggingface`.

        :param path:
        :param args:
        :param process_exclusion: Whether to load checkpoints one by one to 
            save memory.
        :return: state_dict. Note that the state_dict should be processed
            properly to match the current rank.
        """
        raise NotImplementedError(
            "Every model should implement `load_parallel_state_dict` "
            "to properly load a state dict for the cuurent rank."
        )
    
    @staticmethod
    @abstractmethod
    def save_parallel_state_dict(state_dict: dict, path: str,
                                 args: Arguments,
                                 process_exclusion: bool = False):
        """
        Save ``state_dict`` to ``path``.

        The format of saved state dict should be the same as that of
        `huggingface`.
        """
        raise NotImplementedError(
            "Every model should implement `save_parallel_state_dict` "
            "to properly save a state dict for the cuurent rank."
        )
    
    @classmethod
    def _get_model_cls(cls, args: Union[Arguments, str]):
        model_cls = cls
        if isinstance(args, str) and os.path.exists(args):
            args = load_config(args)
        if cls.__name__ == "BaseModel":
            mod = importlib.import_module(
                ".model", f"collie.models.{args.model_type}")
            classes = inspect.getmembers(mod, inspect.isclass)
            for name, _cls in classes:
                if not issubclass(_cls, BaseModel):
                    continue
                if name.lower().startswith(args.model_type):
                    model_cls = _cls
                    break
            if model_cls.__name__ == cls.__name__:
                raise ValueError(
                    f"Unexpected model type `{args.model_type}`"
                )
        else:
            if not cls.__name__.lower().startswith(args.model_type):
                logger.rank_zero_warning(
                    f"The pretrained model's type {args.model_type} does not "
                    f"match the current model {cls.__name__}."
                )
        return model_cls