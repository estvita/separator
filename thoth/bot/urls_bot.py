from django.urls import path
from .views import bot_form_view, bot_list_view, bot_delete

app_name = "bot"

urlpatterns = [
    path('', bot_list_view, name='bot_list'),  # Список ботов
    path('<int:bot_id>/', bot_form_view, name='bot_edit'),  # Редактирование бота
    path('new/', bot_form_view, name='bot_new'),  # Создание нового бота
    path('<int:bot_id>/delete/', bot_delete, name='bot_delete'),  # Удаление бота
]
