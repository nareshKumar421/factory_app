from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .models import LabourVerificationRequest, DepartmentLabourResponse
from .serializers import (
    LabourVerificationRequestListSerializer,
    LabourVerificationRequestDetailSerializer,
    DepartmentLabourResponseSerializer,
    SubmitLabourResponseSerializer,
)
from .permissions import (
    CanCreateVerificationRequest,
    CanViewVerificationRequest,
    CanCloseVerificationRequest,
    CanSubmitDepartmentLabour,
)
from .services.verification_service import VerificationService


class CreateVerificationRequestAPI(APIView):
    permission_classes = [IsAuthenticated, CanCreateVerificationRequest]

    def post(self, request):
        verification_request = VerificationService.create_request(
            user=request.user,
            request_obj=request,
        )
        serializer = LabourVerificationRequestDetailSerializer(verification_request)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ListVerificationRequestsAPI(APIView):
    permission_classes = [IsAuthenticated, CanViewVerificationRequest]

    def get(self, request):
        queryset = LabourVerificationRequest.objects.select_related(
            "created_by"
        ).annotate(
            total_departments=Count("responses"),
            submitted_count=Count("responses", filter=Q(responses__status="SUBMITTED")),
            pending_count=Count("responses", filter=Q(responses__status="PENDING")),
            total_labour_count=Sum(
                "responses__labour_count",
                filter=Q(responses__status="SUBMITTED"),
                default=0,
            ),
        )

        # Date filter
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            queryset = queryset.filter(date__gte=date_from)
        if date_to:
            queryset = queryset.filter(date__lte=date_to)

        # Status filter
        req_status = request.query_params.get("status")
        if req_status:
            queryset = queryset.filter(status=req_status)

        # Pagination
        page = int(request.query_params.get("page", 1))
        page_size = min(int(request.query_params.get("page_size", 20)), 100)
        start = (page - 1) * page_size
        end = start + page_size

        total_count = queryset.count()
        results = queryset[start:end]
        serializer = LabourVerificationRequestListSerializer(results, many=True)

        return Response({
            "results": serializer.data,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        })


class TodayVerificationRequestAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.now().date()
        try:
            verification_request = LabourVerificationRequest.objects.prefetch_related(
                "responses__department", "responses__submitted_by"
            ).select_related("created_by").get(date=today)
        except LabourVerificationRequest.DoesNotExist:
            return Response({"exists": False}, status=status.HTTP_200_OK)

        serializer = LabourVerificationRequestDetailSerializer(verification_request)
        return Response({"exists": True, **serializer.data})


class VerificationRequestDetailAPI(APIView):
    permission_classes = [IsAuthenticated, CanViewVerificationRequest]

    def get(self, request, pk):
        try:
            verification_request = LabourVerificationRequest.objects.prefetch_related(
                "responses__department", "responses__submitted_by"
            ).select_related("created_by").get(pk=pk)
        except LabourVerificationRequest.DoesNotExist:
            return Response(
                {"detail": "Verification request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = LabourVerificationRequestDetailSerializer(verification_request)
        return Response(serializer.data)


class CloseVerificationRequestAPI(APIView):
    permission_classes = [IsAuthenticated, CanCloseVerificationRequest]

    def post(self, request, pk):
        verification_request = VerificationService.close_request(
            verification_request_id=pk,
            user=request.user,
        )
        serializer = LabourVerificationRequestDetailSerializer(verification_request)
        return Response(serializer.data)


class SubmitDepartmentResponseAPI(APIView):
    permission_classes = [IsAuthenticated, CanSubmitDepartmentLabour]

    def post(self, request, pk):
        serializer = SubmitLabourResponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        response_obj = VerificationService.submit_response(
            verification_request_id=pk,
            user=request.user,
            data=serializer.validated_data,
        )
        return Response(
            DepartmentLabourResponseSerializer(response_obj).data,
            status=status.HTTP_200_OK,
        )


class MyDepartmentResponseAPI(APIView):
    permission_classes = [IsAuthenticated, CanSubmitDepartmentLabour]

    def get(self, request, pk):
        if not request.user.department_id:
            return Response(
                {"detail": "You are not assigned to any department."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            response_obj = DepartmentLabourResponse.objects.select_related(
                "department", "submitted_by"
            ).get(
                verification_request_id=pk,
                department_id=request.user.department_id,
            )
        except DepartmentLabourResponse.DoesNotExist:
            return Response(
                {"detail": "No response entry found for your department."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = DepartmentLabourResponseSerializer(response_obj)
        return Response(serializer.data)
