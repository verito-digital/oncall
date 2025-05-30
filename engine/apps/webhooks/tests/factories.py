import factory
import pytz

from apps.webhooks.models import PersonalNotificationWebhook, Webhook, WebhookResponse
from common.utils import UniqueFaker


class CustomWebhookFactory(factory.DjangoModelFactory):
    url = factory.Faker("url")
    name = UniqueFaker("sentence", nb_words=3)

    class Meta:
        model = Webhook


class PersonalNotificationWebhookFactory(factory.DjangoModelFactory):
    class Meta:
        model = PersonalNotificationWebhook


class WebhookResponseFactory(factory.DjangoModelFactory):
    timestamp = factory.Faker("date_time", tzinfo=pytz.UTC)

    class Meta:
        model = WebhookResponse
