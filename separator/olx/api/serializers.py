from rest_framework import serializers


class OlxAuthorizationSerializer(serializers.Serializer):
    code = serializers.CharField(
        required=True, error_messages={"required": "Code is required."}
    )
    state = serializers.CharField(
        required=True, error_messages={"required": "State is required."}
    )
