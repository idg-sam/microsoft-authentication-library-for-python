"""This is the asynchronized version of ../oauth2.py"""
import json
import time
from typing import Mapping

from ..oauth2 import AbstractBaseClient


async def _get_content(http_resp):
    # We accept a coroutine function (i.e. aiohttp) or a plaintext (httpx)
    return await http_resp.text() if callable(http_resp.text) else http_resp.text


class BaseClient(AbstractBaseClient):
    # Sadly, the code between sync and async client duplicates a lot.

    async def _obtain_token(  # The verb "obtain" is influenced by OAUTH2 RFC 6749
            self, grant_type,
            params: Mapping[str, str] = None,  # a dict to be sent as query string to the endpoint
            data=None,  # All relevant data, which will go into the http body
            headers=None,  # a dict to be sent as request headers
            timeout=None,
            post=None,  # A callable to replace requests.post(), for testing.
                        # Such as: lambda url, **kwargs:
                        #   Mock(status_code=200, text="{}")
            **kwargs  # Relay all extra parameters to underlying requests
            ):  # Returns the json object came from the OAUTH2 response
        resp = await (post or self.session.post)(
                self.configuration["token_endpoint"],
                timeout=timeout or self.timeout,
                **dict(kwargs, **self._prepare_token_request(
                    grant_type, params=params, data=data, headers=headers))
                )
        # RFC defines and some uses "status_code", aiohttp uses "status"
        status_code = getattr(resp, "status_code", None) or resp.status
        if status_code >= 500:
            resp.raise_for_status()  # TODO: Will probably retry here
        return self._parse_token_resposne(await _get_content(resp))

    async def obtain_token_by_refresh_token(
            self,
            refresh_token: str,
            scope=None,
            **kwargs):
        # type: (str, Union[str, list, set, tuple]) -> dict
        """Obtain an access token via a refresh token.

        :param refresh_token: The refresh token issued to the client
        :param scope: If omitted, is treated as equal to the scope originally
            granted by the resource ownser,
            according to https://tools.ietf.org/html/rfc6749#section-6
        """
        assert isinstance(refresh_token, string_types)
        data = kwargs.pop('data', {})
        data.update(refresh_token=refresh_token, scope=scope)
        return await self._obtain_token("refresh_token", data=data, **kwargs)

    async def initiate_device_flow(self, scope=None, timeout=None, **kwargs):
        # type: (list, **dict) -> dict
        # The naming of this method is following the wording of this specs
        # https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.1
        """Initiate a device flow.

        Returns the data defined in Device Flow specs.
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.2

        You should then orchestrate the User Interaction as defined in here
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.3

        And possibly here
        https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.3.1
        """
        DAE = "device_authorization_endpoint"
        if not self.configuration.get(DAE):
            raise ValueError("You need to provide device authorization endpoint")
        _data = kwargs.pop("data", {})
        _data.update(client_id=self.client_id, scope=self._stringify(scope or []))
        resp = await self.session.post(
            self.configuration[DAE],
            data=_data,
            timeout=timeout or self.timeout,
            headers=dict(self.default_headers, **kwargs.pop("headers", {})),
            **kwargs)
        flow = json.loads(await _get_content(resp))
        flow["interval"] = int(flow.get("interval", 5))  # Some IdP returns string
        flow["expires_in"] = int(flow.get("expires_in", 1800))
        flow["expires_at"] = time.time() + flow["expires_in"]  # We invent this
        return flow

    async def _obtain_token_by_device_flow(self, flow, **kwargs):
        # type: (dict, **dict) -> dict
        # This method updates flow during each run. And it is non-blocking.
        now = time.time()
        skew = 1
        if flow.get("latest_attempt_at", 0) + flow.get("interval", 5) - skew > now:
            warnings.warn('Attempted too soon. Please do sleep(flow["interval"])')
        data = kwargs.pop("data", {})
        data.update({
            "client_id": self.client_id,
            self.DEVICE_FLOW["DEVICE_CODE"]: flow["device_code"],
            })
        result = await self._obtain_token(
            self.DEVICE_FLOW["GRANT_TYPE"], data=data, **kwargs)
        if result.get("error") == "slow_down":
            # Honor https://tools.ietf.org/html/draft-ietf-oauth-device-flow-12#section-3.5
            flow["interval"] = flow.get("interval", 5) + 5
        flow["latest_attempt_at"] = now
        return result

    async def obtain_token_by_device_flow(self,
            flow,
            exit_condition=lambda flow: flow.get("expires_at", 0) < time.time(),
            sleeper=None,
            **kwargs):
        # type: (dict, Callable) -> dict
        """Obtain token by a device flow object, with customizable polling effect.

        Args:
            flow (dict):
                An object previously generated by initiate_device_flow(...).
                Its content WILL BE CHANGED by this method during each run.
                We share this object with you, so that you could implement
                your own loop, should you choose to do so.

            sleeper (Callable):
                A callable to sleep. Supply the sleep() function of your async
                framework, such as asyncio.sleep().

            exit_condition (Callable):
                This method implements a loop to provide polling effect.
                The loop's exit condition is calculated by this callback.

                The default callback makes the loop run until the flow expires.
                Therefore, one of the ways to exit the polling early,
                is to change the flow["expires_at"] to a small number such as 0.

                In case you are doing async programming, you may want to
                completely turn off the loop. You can do so by using a callback as:

                    exit_condition = lambda flow: True

                to make the loop run only once, i.e. no polling, hence non-block.
        """
        if not sleeper:
            raise ValueError("You need to provide something like asyncio.sleep")
        while True:
            result = await self._obtain_token_by_device_flow(flow, **kwargs)
            if result.get("error") not in self.DEVICE_FLOW_RETRIABLE_ERRORS:
                return result
            for i in range(flow.get("interval", 5)):  # Wait interval seconds
                if exit_condition(flow):
                    return result
                await sleeper(1)  # Shorten each round, to make exit more responsive


class Client(BaseClient):

    async def obtain_token_by_username_password(
            self, username, password, scope=None, **kwargs):
        """The Resource Owner Password Credentials Grant, used by legacy app."""
        data = kwargs.pop("data", {})
        data.update(username=username, password=password, scope=scope)
        return await self._obtain_token("password", data=data, **kwargs)

    async def obtain_token_for_client(self, scope=None, **kwargs):
        """Obtain token for this client (rather than for an end user),
        a.k.a. the Client Credentials Grant, used by Backend Applications.

        We don't name it obtain_token_by_client_credentials(...) because those
        credentials are typically already provided in class constructor, not here.
        You can still explicitly provide an optional client_secret parameter,
        or you can provide such extra parameters as `default_body` during the
        class initialization.
        """
        data = kwargs.pop("data", {})
        data.update(scope=scope)
        return await self._obtain_token("client_credentials", data=data, **kwargs)

