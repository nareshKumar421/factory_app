import logging

from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import ChangePasswordSerializer, LoginSerializer, MeSerializer, UserSerializer

from .models import Department, User
from .serializers import DepartmentSerializer

logger = logging.getLogger(__name__)

class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer

class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user

        if not user.check_password(serializer.validated_data["old_password"]):
            return Response(
                {"detail": "Old password is incorrect"},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(serializer.validated_data["new_password"])
        user.save()

        return Response({"message": "Password changed successfully"})



class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = MeSerializer(request.user)
        return Response(serializer.data)


class CustomTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)

        try:
            if isinstance(response.data, dict):
                response.data["token"] = {
                    "access_expires_in": settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
                    "refresh_expires_in": settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds(),
                }
        except (KeyError, AttributeError) as e:
            # Log configuration errors - SIMPLE_JWT settings may be misconfigured
            logger.error(
                f"Failed to add token expiry info to response: {type(e).__name__}: {e}. "
                "Check SIMPLE_JWT settings in configuration."
            )
        except TypeError as e:
            # Log type errors - lifetime values may not support total_seconds()
            logger.error(
                f"Invalid token lifetime configuration: {type(e).__name__}: {e}. "
                "Ensure ACCESS_TOKEN_LIFETIME and REFRESH_TOKEN_LIFETIME are timedelta objects."
            )

        return response
    

class DepartmentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):


        departments = Department.objects.all()
        serializer = DepartmentSerializer(departments, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = DepartmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    

class UserListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = User.objects.all()
        serializer = UserSerializer(users, many=True)
        return Response(serializer.data)
    
