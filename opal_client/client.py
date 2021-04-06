from logging import disable
import os
import signal
import asyncio
import functools
from typing import List

from fastapi import FastAPI
import websockets

from opal_common.logger import logger, configure_logs
from opal_common.middleware import configure_middleware
from opal_client.config import PolicyStoreTypes, opal_client_config
from opal_client.data.api import init_data_router
from opal_client.data.updater import DataUpdater
from opal_client.policy_store.base_policy_store_client import BasePolicyStoreClient
from opal_client.policy_store.policy_store_client_factory import PolicyStoreClientFactory
from opal_client.opa.runner import OpaRunner
from opal_client.opa.options import OpaServerOptions
from opal_client.policy.api import init_policy_router
from opal_client.policy.updater import PolicyUpdater, update_policy


class OpalClient:
    def __init__(
        self,
        policy_store_type:PolicyStoreTypes=None,
        policy_store:BasePolicyStoreClient=None,
        data_updater:DataUpdater=None,
        data_topics: List[str] = None,
        policy_updater:PolicyUpdater=None,
        inline_opa_enabled:bool=None,
        inline_opa_options:OpaServerOptions=None,
    ) -> None:
        """
        Args:
            policy_store_type (PolicyStoreTypes, optional): [description]. Defaults to POLICY_STORE_TYPE.

            Internal components (for each pass None for default init, or False to disable):
                policy_store (BasePolicyStoreClient, optional): The policy store client. Defaults to None.
                data_updater (DataUpdater, optional): Defaults to None.
                policy_updater (PolicyUpdater, optional): Defaults to None.
        """
        # defaults
        policy_store_type:PolicyStoreTypes=policy_store_type or opal_client_config.POLICY_STORE_TYPE
        inline_opa_enabled:bool=inline_opa_enabled or opal_client_config.INLINE_OPA_ENABLED
        inline_opa_options:OpaServerOptions=inline_opa_options or opal_client_config.INLINE_OPA_CONFIG
        # set logs
        configure_logs()
        # Init policy store client
        self.policy_store_type:PolicyStoreTypes = policy_store_type
        self.policy_store:BasePolicyStoreClient = policy_store or PolicyStoreClientFactory.create(policy_store_type)
        # Init policy updater
        self.policy_updater = policy_updater if policy_updater is not None else PolicyUpdater(policy_store=self.policy_store)
        # Data updating service
        if data_updater is not None:
            self.data_updater = data_updater
        else:
            data_topics = data_topics if data_topics is not None else opal_client_config.DATA_TOPICS
            self.data_updater = DataUpdater(policy_store=self.policy_store, data_topics=data_topics)

        # Internal services
        # Policy store
        if self.policy_store_type == PolicyStoreTypes.OPA and inline_opa_enabled:
            self.opa_runner = OpaRunner.setup_opa_runner(
                options=inline_opa_options,
                rehydration_callbacks=[
                    # refetches policy code (e.g: rego) and static data from server
                    functools.partial(update_policy, policy_store=self.policy_store, force_full_update=True),
                    functools.partial(self.data_updater.get_base_policy_data, data_fetch_reason="policy store rehydration"),
                ]
            )
        else:
            self.opa_runner = False

        # init fastapi app
        self.app: FastAPI = self._init_fast_api_app()

    def _init_fast_api_app(self):
        """
        inits the fastapi app object
        """
        app = FastAPI(
            title="OPAL Client",
            description="OPAL is an administration layer for Open Policy Agent (OPA), detecting changes" + \
            " to both policy and data and pushing live updates to your agents. The opal client is" + \
            " deployed alongside a policy-store (e.g: OPA), keeping it up-to-date, by connecting to" + \
            " an opal-server and subscribing to pub/sub updates for policy and policy data changes.",
            version="0.1.0"
        )
        configure_middleware(app)
        self._configure_api_routes(app)
        self._configure_lifecycle_callbacks(app)
        return app

    def _configure_api_routes(self, app: FastAPI):
        """
        mounts the api routes on the app object
        """
        # Init api routers with required dependencies
        policy_router = init_policy_router(policy_store=self.policy_store)
        data_router = init_data_router(data_updater=self.data_updater)

        # mount the api routes on the app object
        app.include_router(policy_router, tags=["Policy Updater"])
        app.include_router(data_router, tags=["Data Updater"])

        # top level routes (i.e: healthchecks)
        @app.get("/healthcheck", include_in_schema=False)
        @app.get("/", include_in_schema=False)
        def healthcheck():
            return {"status": "ok"}
        return app

    def _configure_lifecycle_callbacks(self, app: FastAPI):
        """
        registers callbacks on app startup and shutdown.

        on app startup we launch our long running processes (async tasks)
        on the event loop. on app shutdown we stop these long running tasks.
        """
        @app.on_event("startup")
        async def startup_event():
            asyncio.create_task(self.start_client_background_tasks())

        @app.on_event("shutdown")
        async def shutdown_event():
            await self.stop_client_background_tasks()

        return app

    async def start_client_background_tasks(self):
        """
        Launch OPAL client long-running tasks:
        - Policy Store runner (e.g: Opa Runner)
        - Policy Updater
        - Data Updater

        If there is a policy store to run, we wait until its up before launching dependent tasks.
        """
        if self.opa_runner:
            # runs the policy store dependent tasks after policy store is up
            self.opa_runner.register_opa_initial_start_callbacks([self.launch_policy_store_dependent_tasks])
            async with self.opa_runner:
                await self.opa_runner.wait_until_done()
        else:
            # we do not run the policy store in the same container
            # therefore we can immediately launch dependent tasks
            await self.launch_policy_store_dependent_tasks()

    async def stop_client_background_tasks(self):
        """
        stops all background tasks (called on shutdown event)
        """
        logger.info("stopping background tasks...")

        # stopping opa runner
        if self.opa_runner:
            await self.opa_runner.stop()

        # stopping updater tasks (each updater runs a pub/sub client)
        logger.info("trying to shutdown DataUpdater and PolicyUpdater gracefully...")
        tasks: List[asyncio.Task] = []
        if self.data_updater:
            tasks.append(asyncio.create_task(self.data_updater.stop()))
        if self.policy_updater:
            tasks.append(asyncio.create_task(self.policy_updater.stop()))

        # disconnect might hang if client is currently in the middle of __connect__
        # so we put a time limit, and then we let uvicorn kill the worker.
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            logger.info("timeout while waiting for DataUpdater and PolicyUpdater to disconnect")

    async def launch_policy_store_dependent_tasks(self):
        try:
            for task in asyncio.as_completed([self.launch_policy_updater(), self.launch_data_updater()]):
                await task
        except websockets.exceptions.InvalidStatusCode as err:
            logger.error("Failed to launch background task -- {err}", err=err)
            logger.info("triggering shutdown with SIGTERM...")
            # this will send SIGTERM (Keyboard interrupt) to the worker, making uvicorn
            # send "lifespan.shutdown" event to Starlette via the ASGI lifespan interface.
            # Starlette will then trigger the @app.on_event("shutdown") callback, which
            # in our case (self.stop_client_background_tasks()) will gracefully shutdown
            # the background processes and only then will terminate the worker.
            os.kill(os.getpid(), signal.SIGTERM)

    async def launch_policy_updater(self):
        if self.policy_updater:
            async with self.policy_updater:
                await self.policy_updater.wait_until_done()

    async def launch_data_updater(self):
        if self.data_updater:
            async with self.data_updater:
                await self.data_updater.wait_until_done()