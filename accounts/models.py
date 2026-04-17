from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from .managers import UserManager



class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model for the application.
    1. email: An EmailField that stores the user's email address. It is unique and serves as the primary identifier for authentication.
    2. full_name: A CharField that stores the user's full name with a maximum length of 150 characters.
    3. employee_code: A CharField that stores a unique code assigned to each employee
    4. is_active: A BooleanField that indicates whether the user's account is active. Defaults to True.
    5. is_staff: A BooleanField that indicates whether the user has staff privileges. Defaults to False.
    6. date_joined: A DateTimeField that records the date and time when the user account was created. It defaults to the current time.
    7. USERNAME_FIELD: A string that specifies the field used for authentication, which is set to "email".
    8. REQUIRED_FIELDS: A list of fields that are required when creating a user via the createsuperuser management command. It includes "full_name" and "employee_code".
    9. objects: An instance of UserManager that provides custom user management functionality.
    10. __str__ method: A method that returns the string representation of the user, which is the user's email address.
    """
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150)
    employee_code = models.CharField(max_length=50, unique=True)

    department = models.ForeignKey(
        'Department',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='users'
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)


    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name", "employee_code"]

    objects = UserManager()

    def __str__(self):
        return self.email
    

class Department(models.Model):
    """
    Department model representing different departments within the organization.
    1. name: A CharField that stores the name of the department with a maximum length of 100 characters.
    2. description: A TextField that provides a description of the department. It is optional and can be left blank.
    3. __str__ method: A method that returns the string representation of the department, which is the department's name.
    """
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


