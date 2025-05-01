def get_tools_for_bot(user, bot, engine):
    tools = []

    features = bot.features.filter(type="function", engine=engine)

    for feature in features:
        # Добавляем публичные функции или приватные, владельцем которых является пользователь
        if feature.privacy == "public" or feature.owner == user:
            # Если description_openai уже является списком (например, [{"type": "function", ...}, ...])
            if isinstance(feature.description_openai, list):
                tools.extend(feature.description_openai)
            else:
                tools.append(feature.description_openai)

    return tools