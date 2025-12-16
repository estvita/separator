from django.urls import path
from .views import BotListView, BotEditView, ConnectorListView, ConnectorEditView

urlpatterns = [
    path('', BotListView.as_view(), name='bitbot_list'),
    path('add/', BotEditView.as_view(), name='bitbot_add'),
    path('<int:pk>/edit/', BotEditView.as_view(), name='bitbot_edit'),

    path('connectors/', ConnectorListView.as_view(), name='connector_list'),
    path('connectors/add/', ConnectorEditView.as_view(), name='connector_add'),
    path('connectors/<int:pk>/edit/', ConnectorEditView.as_view(), name='connector_edit'),
]