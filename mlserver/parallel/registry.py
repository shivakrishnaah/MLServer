import asyncio
import os
import signal

from typing import Optional, Dict, List

from ..settings import ModelSettings
from ..utils import to_absolute_path
from ..model import MLModel
from ..settings import Settings
from ..env import Environment, compute_hash_of_file, compute_hash_of_string
from ..registry import model_initialiser

from .errors import EnvironmentNotFound
from .logging import logger
from .pool import InferencePool, InferencePoolHook

ENV_HASH_ATTR = "__env_hash__"


def _set_environment_hash(model: MLModel, env_hash: Optional[str]):
    setattr(model, ENV_HASH_ATTR, env_hash)


def _get_environment_hash(model: MLModel) -> Optional[str]:
    return getattr(model, ENV_HASH_ATTR, None)


def _get_env_tarball(model: MLModel) -> Optional[str]:
    model_settings = model.settings
    if model_settings.parameters is None:
        return None

    env_tarball = model_settings.parameters.environment_tarball
    if env_tarball is None:
        return None

    return to_absolute_path(model_settings, env_tarball)


def _append_gid_environment_hash(
    env_hash: str, inference_pool_gid: Optional[str] = None
) -> str:
    return f"{env_hash}-{inference_pool_gid}"


class InferencePoolRegistry:
    """
    Keeps track of the different inference pools loaded in the server.
    Each inference pool will generally be used to load a different environment.
    """

    def __init__(
        self, settings: Settings, on_worker_stop: List[InferencePoolHook] = []
    ):
        self._settings = settings
        self._on_worker_stop = on_worker_stop
        self._default_pool = InferencePool(
            self._settings, on_worker_stop=on_worker_stop
        )
        self._pools: Dict[str, InferencePool] = {}

        os.makedirs(self._settings.environments_dir, exist_ok=True)

        # Register sigchld signal handler (saving original to restore it later)
        self._original_sigchld_handler = signal.getsignal(signal.SIGCHLD)
        signal.signal(
            signal.SIGCHLD,
            lambda *args: asyncio.create_task(self._handle_worker_stop(*args)),
        )

    async def _handle_worker_stop(self, signum, frame):
        pid, exit_code = os.waitpid(-1, os.WNOHANG)
        if pid == 0 or exit_code == 0:
            # Worker terminated gracefully, nothing to do
            return

        await self._default_pool.on_worker_stop(pid, exit_code)
        await asyncio.gather(
            *[pool.on_worker_stop(pid, exit_code) for pool in self._pools.values()]
        )

    async def _get_or_create(self, model: MLModel) -> InferencePool:
        if (
            model.settings.parameters is not None
            and model.settings.parameters.environment_path
        ):
            pool = await self._get_or_create_with_existing_env(
                model.settings.parameters.environment_path,
                model.settings.parameters.inference_pool_gid,
            )
        else:
            pool = await self._get_or_create_with_tarball(model)
        return pool

    async def _get_or_create_with_existing_env(
        self,
        environment_path: str,
        inference_pool_gid: Optional[str],
    ) -> InferencePool:
        """
        Creates or returns the InferencePool for a model that uses an existing
        python environment.
        """
        expanded_environment_path = os.path.abspath(
            os.path.expanduser(os.path.expandvars(environment_path))
        )
        logger.info(f"Using environment {expanded_environment_path}")
        env_hash = await compute_hash_of_string(expanded_environment_path)

        if inference_pool_gid is not None:
            env_hash = _append_gid_environment_hash(env_hash, inference_pool_gid)

        if env_hash in self._pools:
            return self._pools[env_hash]

        env = Environment(
            env_path=expanded_environment_path,
            env_hash=env_hash,
            delete_env=False,
        )
        pool = InferencePool(
            self._settings, env=env, on_worker_stop=self._on_worker_stop
        )
        self._pools[env_hash] = pool
        return pool

    async def _get_or_create_with_tarball(self, model: MLModel) -> InferencePool:
        """
        Creates or returns the InferencePool for a model that uses a
        tarball as a Python environment.
        """
        env_tarball = _get_env_tarball(model)
        inference_pool_gid = (
            model.settings.parameters.inference_pool_gid
            if model.settings.parameters
            else None
        )

        if not env_tarball:
            return (
                self._pools.setdefault(
                    inference_pool_gid,
                    InferencePool(self._settings, on_worker_stop=self._on_worker_stop),
                )
                if inference_pool_gid
                else self._default_pool
            )

        env_hash = await compute_hash_of_file(env_tarball)
        if inference_pool_gid is not None:
            env_hash = _append_gid_environment_hash(env_hash, inference_pool_gid)

        if env_hash in self._pools:
            return self._pools[env_hash]

        env = await self._extract_tarball(env_hash, env_tarball)
        self._pools[env_hash] = InferencePool(
            self._settings, env=env, on_worker_stop=self._on_worker_stop
        )

        return self._pools[env_hash]

    async def _extract_tarball(self, env_hash: str, env_tarball: str) -> Environment:
        env_path = self._get_env_path(env_hash)
        if os.path.isdir(env_path):
            # If env has already been extracted, use that
            return Environment(env_path, env_hash)

        os.makedirs(env_path)
        return await Environment.from_tarball(env_tarball, env_path, env_hash)

    def _get_env_path(self, env_hash: str) -> str:
        return os.path.join(self._settings.environments_dir, env_hash)

    async def _find(self, model: MLModel) -> InferencePool:
        env_hash = _get_environment_hash(model)
        inference_pool_gid = (
            model.settings.parameters.inference_pool_gid
            if model.settings.parameters
            else None
        )

        if not env_hash:
            if not inference_pool_gid:
                return self._default_pool
            else:
                return self._pools[inference_pool_gid]

        if env_hash not in self._pools:
            raise EnvironmentNotFound(model, env_hash)

        return self._pools[env_hash]

    def _should_load_model(self, model_settings: ModelSettings):
        if model_settings.parallel_workers is not None:
            logger.warning(
                "DEPRECATED!! The `parallel_workers` setting at the model-level "
                "has now been deprecated and moved "
                "to the top-level server "
                "settings. "
                "This field will be removed in MLServer 1.2.0. "
                "To access the new field, you can either update the "
                "`settings.json` file, or update the `MLSERVER_PARALLEL_WORKERS` "
                "environment variable. "
                f"The current value of the server-level's `parallel_workers` field is "
                f"'{self._settings.parallel_workers}'."
            )

            # NOTE: This is a remnant from the previous architecture for parallel
            # workers, where each worker had its own pool.
            # For backwards compatibility, we will respect when a model disables
            # parallel inference.
            if model_settings.parallel_workers <= 0:
                return False

        if not self._settings.parallel_workers:
            return False

        return True

    def model_initialiser(self, model_settings: ModelSettings) -> MLModel:
        """
        Used to initialise a model object in the ModelRegistry.
        """
        if not self._should_load_model(model_settings):
            # If parallel inference should not be used, instantiate the model
            # as normal.
            return model_initialiser(model_settings)

        parameters = model_settings.parameters
        if not parameters or not parameters.environment_tarball:
            # If model is not using a custom environment, instantiate the model
            # as normal.
            return model_initialiser(model_settings)

        # Otherwise, return a dummy model for now and wait for the load_model
        # hook to create the actual thing.
        # This avoids instantiating the model's actual class within the
        # main process.
        return MLModel(model_settings)

    async def load_model(self, model: MLModel) -> MLModel:
        if not self._should_load_model(model.settings):
            # Skip load if model has disabled parallel workers
            return model

        # TODO: If load fails, should we remove pool if empty?
        pool = await self._get_or_create(model)
        loaded = await pool.load_model(model)
        _set_environment_hash(loaded, pool.env_hash)
        return loaded

    async def reload_model(self, old_model: MLModel, new_model: MLModel) -> MLModel:
        if not self._should_load_model(new_model.settings):
            # TODO: What would happen if old_model had parallel inference
            # enabled and is disabled in new_model (and viceversa)?
            # Skip reload if model has disabled parallel workers
            return new_model

        old_hash = _get_environment_hash(old_model)
        new_pool = await self._get_or_create(new_model)

        loaded = await new_pool.reload_model(old_model, new_model)
        _set_environment_hash(loaded, new_pool.env_hash)
        if old_hash != new_pool.env_hash:
            # Environment has changed in the new version, so unload the old one
            await self.unload_model(old_model)

        return loaded

    async def unload_model(self, model: MLModel) -> MLModel:
        if not self._should_load_model(model.settings):
            # Skip unload if model has disabled parallel workers
            return model

        pool = await self._find(model)
        unloaded = await pool.unload_model(model)

        if pool != self._default_pool and pool.empty():
            logger.info(f"Inference pool with hash '{pool.env_hash}' is now empty")
            await self._close_pool(pool.env_hash)

        return unloaded

    async def close(self):
        # Reset signal handler
        signal.signal(signal.SIGCHLD, self._original_sigchld_handler)
        await asyncio.gather(
            self._close_pool(None),
            *[self._close_pool(env_hash) for env_hash in self._pools],
        )

    async def _close_pool(self, env_hash: Optional[str] = None):
        pool = self._default_pool
        if env_hash:
            pool = self._pools[env_hash]

        logger.info(f"Waiting for shutdown of {pool.name}...")
        await pool.close()
        logger.info(f"Shutdown of {pool.name} complete")

        if env_hash:
            # force calling __del__ on `Environment` to clean up
            self._pools[env_hash]._env = None  # pylint: disable=protected-access
            del self._pools[env_hash]
