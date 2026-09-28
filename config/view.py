from rest_framework.views import APIView
from rest_framework.response import Response        


class RootApiView(APIView):
    def get(self, request):
        return Response({
            "message": "Welcome to the Accounts API",
            "endpoints": {
                "login": "/login/",
                "change_password": "/change-password/",
                "token_refresh": "/token/refresh/",
                "me": "/me/",
                "departments": "/departments/",
            }
        })

class HealthView(APIView):
    """GET /api/v1/health/ — for monitoring: is the app up and can it reach its
    own database? No login and no SAP call (SAP being down is not the app being
    down), and nothing about the setup is revealed. SAP Portal had /api/health."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = []

    def get(self, request):
        from django.db import DatabaseError, connection

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DatabaseError:
            return Response({"status": "error", "database": "unreachable"}, status=503)
        return Response({"status": "ok", "database": "ok"})
