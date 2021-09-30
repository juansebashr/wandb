"""
pytorch profiler
"""
import os

import wandb
from wandb.errors import Error, UsageError

PYTORCH_MODULE = "torch"
PYTORCH_PROFILER_MODULE = "torch.profiler"


def torch_trace_handler():
    """A function to be used in conjunction with the PyTorch Profiler API, as an argument to
    `torch.profiler.profile(..., on_trace_ready = torch_trace_handler())`.

    Calling this function ensures that profiler charts & tables can be viewed in your run dashboard
    on wandb.ai.

    Please note that `wandb.init()` must be called before this function is invoked.
    The PyTorch (torch) version must also be at least 1.9, in order to ensure stability
    of their Profiler API.

    Args:
        None

    Returns:
        None

    Raises:
        UsageError if wandb.init() hasn't been called before profiling.
        Error if torch version is less than 1.9.0.

    Examples:
    ```python
    run = wandb.init()
    run.config.id = "profile_code"

    with torch.profiler.profile(
        schedule=torch.profiler.schedule(wait=1, warmup=1, active=3, repeat=1),
        on_trace_ready=wandb.profiler.torch_trace_handler(),
        record_shapes=True,
        with_stack=True,
    ) as prof:
        for i, batch in enumerate(dataloader):
            if step >= 5:
                break
            train(batch)
            prof.step()
    ```
    """
    torch = wandb.util.get_module(PYTORCH_MODULE, required=True)
    torch_profiler = wandb.util.get_module(PYTORCH_PROFILER_MODULE, required=True)
    version = tuple(
        map(lambda x: int(x), torch.__version__.replace("+cpu", "").split("."))
    )

    if version < (1, 9, 0):
        raise Error(
            f"torch version must be at least 1.9 in order to use the PyTorch Profiler API.\
            \nVersion of torch currently installed: {torch.__version__}"
        )

    try:
        logdir = os.path.join(wandb.run.dir, "pytorch_traces")
        os.mkdir(logdir)
    except AttributeError:
        raise UsageError(
            "Please call wandb.init() before wandb.profiler.torch_trace_handler()"
        ) from None

    return torch_profiler.tensorboard_trace_handler(logdir)