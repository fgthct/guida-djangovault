from django.urls import path

from .consumers import AvvisiConsumer

websocket_urlpatterns = [path("ws/", AvvisiConsumer.as_asgi())]
