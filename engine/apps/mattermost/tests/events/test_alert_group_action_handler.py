import pytest
from django.utils import timezone

from apps.alerts.constants import ActionSource, AlertGroupState
from apps.alerts.models import AlertReceiveChannel
from apps.mattermost.events.alert_group_actions_handler import AlertGroupActionHandler
from apps.mattermost.events.types import EventAction
from apps.mattermost.models import MattermostMessage
from apps.mattermost.utils import MattermostEventAuthenticator


@pytest.mark.django_db
@pytest.mark.parametrize(
    "event_action,expected_state",
    [
        (EventAction.ACKNOWLEDGE, AlertGroupState.ACKNOWLEDGED),
        (EventAction.RESOLVE, AlertGroupState.RESOLVED),
        (EventAction.UNACKNOWLEDGE, AlertGroupState.FIRING),
        (EventAction.UNRESOLVE, AlertGroupState.FIRING),
    ],
)
def test_alert_group_action_success(
    make_organization_and_user,
    make_alert_receive_channel,
    make_alert_group,
    make_alert,
    make_mattermost_event,
    make_mattermost_message,
    make_mattermost_user,
    event_action,
    expected_state,
):
    organization, user = make_organization_and_user()

    alert_receive_channel = make_alert_receive_channel(
        organization, integration=AlertReceiveChannel.INTEGRATION_GRAFANA
    )

    if event_action in [EventAction.ACKNOWLEDGE, EventAction.RESOLVE]:
        alert_group = make_alert_group(alert_receive_channel)
    elif event_action == EventAction.UNACKNOWLEDGE:
        alert_group = make_alert_group(
            alert_receive_channel=alert_receive_channel,
            acknowledged_at=timezone.now(),
            acknowledged=True,
        )
    elif event_action == EventAction.UNRESOLVE:
        alert_group = make_alert_group(alert_receive_channel, resolved=True)

    make_alert(alert_group=alert_group, raw_request_data=alert_receive_channel.config.example_payload)

    mattermost_message = make_mattermost_message(alert_group, MattermostMessage.ALERT_GROUP_MESSAGE)
    mattermost_user = make_mattermost_user(user=user)

    token = MattermostEventAuthenticator.create_token(organization=organization)
    event = make_mattermost_event(
        event_action,
        token,
        post_id=mattermost_message.post_id,
        channel_id=mattermost_message.channel_id,
        user_id=mattermost_user.mattermost_user_id,
        alert=alert_group.public_primary_key,
    )
    handler = AlertGroupActionHandler(event=event, user=user)
    handler.process()
    alert_group.refresh_from_db()
    assert alert_group.state == expected_state
    assert alert_group.log_records.last().action_source == ActionSource.MATTERMOST


@pytest.mark.django_db
def test_alert_group_not_found(
    make_organization_and_user,
    make_alert_receive_channel,
    make_alert_group,
    make_alert,
    make_mattermost_event,
    make_mattermost_user,
):
    organization, user = make_organization_and_user()

    alert_receive_channel = make_alert_receive_channel(
        organization, integration=AlertReceiveChannel.INTEGRATION_GRAFANA
    )
    alert_group = make_alert_group(alert_receive_channel)
    make_alert(alert_group=alert_group, raw_request_data=alert_receive_channel.config.example_payload)
    mattermost_user = make_mattermost_user(user=user)

    token = MattermostEventAuthenticator.create_token(organization=organization)
    event = make_mattermost_event(
        EventAction.ACKNOWLEDGE, token, user_id=mattermost_user.mattermost_user_id, alert="ABC"
    )
    handler = AlertGroupActionHandler(event=event, user=user)
    handler.process()
    alert_group.refresh_from_db()
    assert not alert_group.acknowledged


@pytest.mark.django_db
def test_alert_group_action_not_found(
    make_organization_and_user,
    make_alert_receive_channel,
    make_alert_group,
    make_alert,
    make_mattermost_event,
    make_mattermost_user,
):
    organization, user = make_organization_and_user()

    alert_receive_channel = make_alert_receive_channel(
        organization, integration=AlertReceiveChannel.INTEGRATION_GRAFANA
    )
    alert_group = make_alert_group(alert_receive_channel)
    make_alert(alert_group=alert_group, raw_request_data=alert_receive_channel.config.example_payload)
    mattermost_user = make_mattermost_user(user=user)

    token = MattermostEventAuthenticator.create_token(organization=organization)
    event = make_mattermost_event("", token, user_id=mattermost_user.mattermost_user_id, alert="ABC")
    handler = AlertGroupActionHandler(event=event, user=user)
    handler.process()
    alert_group.refresh_from_db()
    assert not alert_group.acknowledged
