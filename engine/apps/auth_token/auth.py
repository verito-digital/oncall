import json
import logging
import typing

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.request import Request

from apps.auth_token.grafana.grafana_auth_token import setup_organization
from apps.grafana_plugin.helpers.gcom import check_token
from apps.grafana_plugin.sync_data import SyncPermission, SyncUser
from apps.user_management.exceptions import OrganizationDeletedException, OrganizationMovedException
from apps.user_management.models import User
from apps.user_management.models.organization import Organization
from apps.user_management.sync import get_or_create_user
from common.utils import validate_url
from settings.base import SELF_HOSTED_SETTINGS

from .constants import (
    GOOGLE_OAUTH2_AUTH_TOKEN_NAME,
    MATTERMOST_AUTH_TOKEN_NAME,
    SCHEDULE_EXPORT_TOKEN_NAME,
    SLACK_AUTH_TOKEN_NAME,
)
from .exceptions import InvalidToken
from .models import (
    ApiAuthToken,
    GoogleOAuth2Token,
    IntegrationBacksyncAuthToken,
    MattermostAuthToken,
    PluginAuthToken,
    ScheduleExportAuthToken,
    ServiceAccountToken,
    SlackAuthToken,
    UserScheduleExportAuthToken,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

T = typing.TypeVar("T")


class ServerUser(AnonymousUser):
    @property
    def is_authenticated(self):
        # Always return True. This is a way to tell if
        # the user has been authenticated in permissions
        return True


class ApiTokenAuthentication(BaseAuthentication):
    model = ApiAuthToken

    def authenticate(self, request):
        auth = get_authorization_header(request).decode("utf-8")
        user, auth_token = self.authenticate_credentials(auth)

        if not user.is_active:
            raise exceptions.AuthenticationFailed("Only active users are allowed to perform this action.")

        return user, auth_token

    def authenticate_credentials(self, token):
        """
        Due to the random nature of hashing a  value, this must inspect
        each auth_token individually to find the correct one.
        """
        try:
            auth_token = self.model.validate_token_string(token)
        except InvalidToken:
            raise exceptions.AuthenticationFailed("Invalid token.")

        if auth_token.organization.is_moved:
            raise OrganizationMovedException(auth_token.organization)
        if auth_token.organization.deleted_at:
            raise OrganizationDeletedException(auth_token.organization)

        return auth_token.user, auth_token


class BasePluginAuthentication(BaseAuthentication):
    """
    Authentication used by grafana-plugin app where we tolerate user not being set yet due to being in
    a state of initialization, Only validates that the plugin should be talking to the backend. Outside of
    this app PluginAuthentication should be used since it also checks the user.
    """

    def authenticate_header(self, request):
        # Check parent's method comments
        return "Bearer"

    def authenticate(self, request: Request) -> typing.Tuple[User, PluginAuthToken]:
        token_string = get_authorization_header(request).decode()

        if not token_string:
            raise exceptions.AuthenticationFailed("No token provided")

        return self.authenticate_credentials(token_string, request)

    def authenticate_credentials(self, token_string: str, request: Request) -> typing.Tuple[User, PluginAuthToken]:
        context_string = request.headers.get("X-Instance-Context")
        if not context_string:
            raise exceptions.AuthenticationFailed("No instance context provided.")

        try:
            context = dict(json.loads(context_string))
        except (ValueError, TypeError):
            raise exceptions.AuthenticationFailed("Instance context must be JSON dict.")

        if "stack_id" not in context or "org_id" not in context:
            raise exceptions.AuthenticationFailed("Invalid instance context.")

        try:
            auth_token = check_token(token_string, context=context)
            if not auth_token.organization:
                raise exceptions.AuthenticationFailed("No organization associated with token.")
        except InvalidToken:
            raise exceptions.AuthenticationFailed("Invalid token.")

        user = self._get_user(request, auth_token.organization)
        return user, auth_token

    @staticmethod
    def _get_user(request: Request, organization: Organization) -> User:
        try:
            context = dict(json.loads(request.headers.get("X-Grafana-Context")))
        except (ValueError, TypeError):
            logger.info("auth request user not found - missing valid X-Grafana-Context")
            return None

        if "UserId" not in context and "UserID" not in context:
            logger.info("auth request user not found - X-Grafana-Context missing UserID")
            return None

        try:
            user_id = context["UserId"]
        except KeyError:
            user_id = context["UserID"]

        if context.get("IsServiceAccount", False):
            service_account_role = context.get("Role", "None")
            # no user involved in service account requests
            logger.info(f"serviceaccount request - id={user_id} - role={service_account_role}")
            return None

        try:
            return organization.users.get(user_id=user_id)
        except User.DoesNotExist:
            logger.info(f"auth request user not found - user_id={user_id}")
            return None


class PluginAuthentication(BasePluginAuthentication):
    @staticmethod
    def _get_user(request: Request, organization: Organization) -> User:
        try:
            context = dict(json.loads(request.headers.get("X-Grafana-Context")))
        except (ValueError, TypeError):
            raise exceptions.AuthenticationFailed("Grafana context must be JSON dict.")

        if context.get("IsServiceAccount", False):
            raise exceptions.AuthenticationFailed("Service accounts requests are not allowed.")

        try:
            user_id = context.get("UserId", context.get("UserID"))
            if user_id is not None:
                return organization.users.get(user_id=user_id)
            elif "Login" in context:
                return organization.users.get(username=context["Login"])
            else:
                raise exceptions.AuthenticationFailed("Grafana context must specify a User or UserID.")
        except User.DoesNotExist:
            try:
                user_data = dict(json.loads(request.headers.get("X-Oncall-User-Context")))
            except (ValueError, TypeError):
                raise exceptions.AuthenticationFailed("User context must be JSON dict.")
            if user_data:
                permissions = []
                if user_data.get("permissions"):
                    permissions = [
                        SyncPermission(action=permission["action"]) for permission in user_data["permissions"]
                    ]
                user_sync_data = SyncUser(
                    id=user_data["id"],
                    name=user_data["name"],
                    login=user_data["login"],
                    email=user_data["email"],
                    role=user_data["role"],
                    avatar_url=user_data["avatar_url"],
                    permissions=permissions,
                    teams=user_data.get("teams", None),
                )
                return get_or_create_user(organization, user_sync_data)
            else:
                logger.debug("Could not get user from grafana request.")
                raise exceptions.AuthenticationFailed("Non-existent or anonymous user.")


class PluginAuthenticationSchema(OpenApiAuthenticationExtension):
    target_class = PluginAuthentication
    name = "PluginAuthentication"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": (
                "Additional X-Instance-Context and X-Grafana-Context headers must be set. "
                "THIS WILL NOT WORK IN SWAGGER UI."
            ),
        }


class GrafanaIncidentUser(AnonymousUser):
    @property
    def is_authenticated(self):
        # Always return True. This is a way to tell if
        # the user has been authenticated in permissions
        return True


class GrafanaIncidentStaticKeyAuth(BaseAuthentication):
    def authenticate_header(self, request):  # noqa
        # Check parent's method comments
        return "Bearer"

    def authenticate(self, request: Request) -> typing.Tuple[GrafanaIncidentUser, None]:
        token_string = get_authorization_header(request).decode()

        if (
            not token_string == settings.GRAFANA_INCIDENT_STATIC_API_KEY
            or settings.GRAFANA_INCIDENT_STATIC_API_KEY is None
        ):
            raise exceptions.AuthenticationFailed("Wrong token")

        if not token_string:
            raise exceptions.AuthenticationFailed("No token provided")

        return self.authenticate_credentials(token_string, request)

    def authenticate_credentials(self, token_string: str, request: Request) -> typing.Tuple[GrafanaIncidentUser, None]:
        try:
            user = GrafanaIncidentUser()
        except InvalidToken:
            raise exceptions.AuthenticationFailed("Invalid token.")

        return user, None


class _SocialAuthTokenAuthentication(BaseAuthentication, typing.Generic[T]):
    def authenticate(self, request) -> typing.Optional[typing.Tuple[User, T]]:
        """
        If you don't return `None`, the authenticate will raise an `APIException`, so the next authentication class
        will not be called.
        https://stackoverflow.com/a/61623607/3902555

        This is useful for the social_auth views where we want to use multiple authentication classes
        for the same view.
        """
        auth = request.query_params.get(self.token_query_param_name)
        if not auth:
            return None

        try:
            auth_token = self.model.validate_token_string(auth)
            return auth_token.user, auth_token
        except InvalidToken:
            return None


class SlackTokenAuthentication(_SocialAuthTokenAuthentication[SlackAuthToken]):
    token_query_param_name = SLACK_AUTH_TOKEN_NAME
    model = SlackAuthToken


class MattermostTokenAuthentication(_SocialAuthTokenAuthentication[MattermostAuthToken]):
    token_query_param_name = MATTERMOST_AUTH_TOKEN_NAME
    model = MattermostAuthToken


class GoogleTokenAuthentication(_SocialAuthTokenAuthentication[GoogleOAuth2Token]):
    token_query_param_name = GOOGLE_OAUTH2_AUTH_TOKEN_NAME
    model = GoogleOAuth2Token


class ScheduleExportAuthentication(BaseAuthentication):
    model = ScheduleExportAuthToken

    def authenticate(self, request) -> typing.Tuple[User, ScheduleExportAuthToken]:
        auth = request.query_params.get(SCHEDULE_EXPORT_TOKEN_NAME)
        public_primary_key = request.parser_context.get("kwargs", {}).get("pk")
        if not auth:
            raise exceptions.AuthenticationFailed("Invalid token.")

        auth_token = self.authenticate_credentials(auth, public_primary_key)
        return auth_token

    def authenticate_credentials(
        self, token_string: str, public_primary_key: str
    ) -> typing.Tuple[User, ScheduleExportAuthToken]:
        try:
            auth_token = self.model.validate_token_string(token_string)
        except InvalidToken:
            raise exceptions.AuthenticationFailed("Invalid token.")

        if auth_token.organization.is_moved:
            raise OrganizationMovedException(auth_token.organization)
        if auth_token.organization.deleted_at:
            raise OrganizationDeletedException(auth_token.organization)

        if auth_token.schedule.public_primary_key != public_primary_key:
            raise exceptions.AuthenticationFailed("Invalid schedule export token for schedule")

        if not auth_token.active:
            raise exceptions.AuthenticationFailed("Export token is deactivated")

        return auth_token.user, auth_token


class UserScheduleExportAuthentication(BaseAuthentication):
    model = UserScheduleExportAuthToken

    def authenticate(self, request) -> typing.Tuple[User, UserScheduleExportAuthToken]:
        auth = request.query_params.get(SCHEDULE_EXPORT_TOKEN_NAME)
        public_primary_key = request.parser_context.get("kwargs", {}).get("pk")

        if not auth:
            raise exceptions.AuthenticationFailed("Invalid token.")

        auth_token = self.authenticate_credentials(auth, public_primary_key)
        return auth_token

    def authenticate_credentials(
        self, token_string: str, public_primary_key: str
    ) -> typing.Tuple[User, UserScheduleExportAuthToken]:
        try:
            auth_token = self.model.validate_token_string(token_string)
        except InvalidToken:
            raise exceptions.AuthenticationFailed("Invalid token")

        if auth_token.organization.is_moved:
            raise OrganizationMovedException(auth_token.organization)
        if auth_token.organization.deleted_at:
            raise OrganizationDeletedException(auth_token.organization)

        if auth_token.user.public_primary_key != public_primary_key:
            raise exceptions.AuthenticationFailed("Invalid schedule export token for user")

        if not auth_token.active:
            raise exceptions.AuthenticationFailed("Export token is deactivated")

        return auth_token.user, auth_token


X_GRAFANA_URL = "X-Grafana-URL"
X_GRAFANA_INSTANCE_ID = "X-Grafana-Instance-ID"


class GrafanaServiceAccountAuthentication(BaseAuthentication):
    def authenticate(self, request):
        auth = get_authorization_header(request).decode("utf-8")
        if not auth:
            raise exceptions.AuthenticationFailed("Invalid token.")
        if not auth.startswith(ServiceAccountToken.GRAFANA_SA_PREFIX):
            return None

        organization = self.get_organization(request, auth)
        if not organization:
            raise exceptions.AuthenticationFailed("Organization not found.")
        if organization.is_moved:
            raise OrganizationMovedException(organization)
        if organization.deleted_at:
            raise OrganizationDeletedException(organization)

        return self.authenticate_credentials(organization, auth)

    def get_organization(self, request, auth):
        grafana_url = request.headers.get(X_GRAFANA_URL)
        if grafana_url:
            url = validate_url(grafana_url)
            if url is not None:
                url = url.rstrip("/")
                organization = Organization.objects.filter(grafana_url=url).first()
                if not organization:
                    # trigger a request to sync the organization
                    # (ignore response since we can get a 400 if sync was already triggered;
                    # if organization exists, we are good)
                    setup_organization(url, auth)
                    organization = Organization.objects.filter(grafana_url=url).first()
                    if organization is None:
                        # sync may still be in progress, client should retry
                        raise exceptions.Throttled(detail="Organization being synced, please retry.")
                return organization

        if settings.LICENSE == settings.CLOUD_LICENSE_NAME:
            instance_id = request.headers.get(X_GRAFANA_INSTANCE_ID)
            if not instance_id:
                raise exceptions.AuthenticationFailed(f"Missing {X_GRAFANA_INSTANCE_ID}")
            return Organization.objects.filter(stack_id=instance_id).first()
        else:
            org_slug = SELF_HOSTED_SETTINGS["ORG_SLUG"]
            instance_slug = SELF_HOSTED_SETTINGS["STACK_SLUG"]
            return Organization.objects.filter(org_slug=org_slug, stack_slug=instance_slug).first()

    def authenticate_credentials(self, organization, token):
        try:
            user, auth_token = ServiceAccountToken.validate_token(organization, token)
        except InvalidToken:
            raise exceptions.AuthenticationFailed("Invalid token.")

        return user, auth_token


class IntegrationBacksyncAuthentication(BaseAuthentication):
    model = IntegrationBacksyncAuthToken

    def authenticate(self, request) -> typing.Tuple[ServerUser, IntegrationBacksyncAuthToken]:
        token = get_authorization_header(request).decode("utf-8")

        if not token:
            raise exceptions.AuthenticationFailed("Invalid token.")

        return self.authenticate_credentials(token)

    def authenticate_credentials(self, token_string: str) -> typing.Tuple[ServerUser, IntegrationBacksyncAuthToken]:
        try:
            auth_token = self.model.validate_token_string(token_string)
        except InvalidToken:
            raise exceptions.AuthenticationFailed("Invalid token")

        if auth_token.organization.is_moved:
            raise OrganizationMovedException(auth_token.organization)
        if auth_token.organization.deleted_at:
            raise OrganizationDeletedException(auth_token.organization)

        user = ServerUser()

        return user, auth_token
