"""
Serializers for the academic administrative API.
"""

from __future__ import annotations

from rest_framework import serializers

from academic.models import Course, Department
from academic.services import normalize_course_code, normalize_email, normalize_person_name


class DepartmentReadSerializer(serializers.ModelSerializer):
    """
    Read serializer for departments.
    """

    class Meta:
        """
        Serializer field configuration.
        """

        model = Department
        fields = ["id", "name"]
        read_only_fields = fields


class DepartmentWriteSerializer(serializers.Serializer):
    """
    Validate department create and update payloads.
    """

    name = serializers.CharField(max_length=150)

    def validate_name(self, value: str) -> str:
        """
        Normalize and validate the department name.

        Args:
            value: Raw department name.

        Returns:
            str: Normalized department name.

        Raises:
            serializers.ValidationError: If the name is empty after normalization.
        """
        name = normalize_person_name(value)
        if not name:
            raise serializers.ValidationError("Informe o nome do departamento.")
        return name


class CourseWriteSerializer(serializers.Serializer):
    """
    Validate course creation payloads.
    """

    name = serializers.CharField(max_length=150)
    code = serializers.CharField(max_length=20)
    departmentId = serializers.UUIDField(required=False)
    departmentName = serializers.CharField(
        max_length=150, required=False, allow_blank=True
    )
    coordinatorEmail = serializers.EmailField(required=False, allow_blank=True)

    def validate_name(self, value: str) -> str:
        """
        Normalize and validate the course name.

        Args:
            value: Raw course name.

        Returns:
            str: Normalized course name.

        Raises:
            serializers.ValidationError: If the name is empty after normalization.
        """
        name = normalize_person_name(value)
        if not name:
            raise serializers.ValidationError("Informe o nome do curso.")
        return name

    def validate_code(self, value: str) -> str:
        """
        Normalize and validate the course code.

        Args:
            value: Raw course code.

        Returns:
            str: Normalized course code.

        Raises:
            serializers.ValidationError: If the code is empty after normalization.
        """
        code = normalize_course_code(value)
        if not code:
            raise serializers.ValidationError("Informe o código do curso.")
        return code

    def validate_coordinatorEmail(self, value: str) -> str:
        """
        Normalize the coordinator e-mail address.

        Args:
            value: Raw coordinator e-mail.

        Returns:
            str: Normalized e-mail address.
        """
        return normalize_email(value)

    def validate_departmentName(self, value: str) -> str:
        """
        Normalize the department name.

        Args:
            value: Raw department name.

        Returns:
            str: Normalized department name.
        """
        return normalize_person_name(value)

    def validate(self, attrs: dict) -> dict:
        """
        Ensure that a department identifier or name is provided.

        Args:
            attrs: Validated serializer attributes.

        Returns:
            dict: Validated attributes mapped to service parameters.

        Raises:
            serializers.ValidationError: If no department is provided.
        """
        if not attrs.get("departmentId") and not attrs.get("departmentName"):
            raise serializers.ValidationError(
                {"department": "Informe o departamento."}
            )
        return {
            "name": attrs["name"],
            "code": attrs["code"],
            "department_id": str(attrs["departmentId"]) if attrs.get("departmentId") else None,
            "department_name": attrs.get("departmentName") or None,
            "coordinator_email": attrs.get("coordinatorEmail", ""),
        }


class CourseUpdateSerializer(serializers.Serializer):
    """
    Validate partial course update payloads.
    """

    name = serializers.CharField(max_length=150, required=False)
    code = serializers.CharField(max_length=20, required=False)
    departmentId = serializers.UUIDField(required=False)
    departmentName = serializers.CharField(
        max_length=150, required=False, allow_blank=True
    )
    coordinatorEmail = serializers.EmailField(required=False, allow_blank=True)

    def validate_name(self, value: str) -> str:
        """
        Normalize and validate the course name.

        Args:
            value: Raw course name.

        Returns:
            str: Normalized course name.

        Raises:
            serializers.ValidationError: If the name is empty after normalization.
        """
        name = normalize_person_name(value)
        if not name:
            raise serializers.ValidationError("Informe o nome do curso.")
        return name

    def validate_code(self, value: str) -> str:
        """
        Normalize and validate the course code.

        Args:
            value: Raw course code.

        Returns:
            str: Normalized course code.

        Raises:
            serializers.ValidationError: If the code is empty after normalization.
        """
        code = normalize_course_code(value)
        if not code:
            raise serializers.ValidationError("Informe o código do curso.")
        return code

    def validate_coordinatorEmail(self, value: str) -> str:
        """
        Normalize the coordinator e-mail address.

        Args:
            value: Raw coordinator e-mail.

        Returns:
            str: Normalized e-mail address.
        """
        return normalize_email(value)

    def validate_departmentName(self, value: str) -> str:
        """
        Normalize the department name.

        Args:
            value: Raw department name.

        Returns:
            str: Normalized department name.
        """
        return normalize_person_name(value)

    def validate(self, attrs: dict) -> dict:
        """
        Map validated fields to service parameters.

        Args:
            attrs: Validated serializer attributes.

        Returns:
            dict: Service keyword arguments.
        """
        result: dict = {}
        if "name" in attrs:
            result["name"] = attrs["name"]
        if "code" in attrs:
            result["code"] = attrs["code"]
        if "departmentId" in attrs:
            result["department_id"] = str(attrs["departmentId"])
        if "departmentName" in attrs and attrs["departmentName"]:
            result["department_name"] = attrs["departmentName"]
        if "coordinatorEmail" in attrs:
            result["coordinator_email"] = attrs["coordinatorEmail"]
        return result
