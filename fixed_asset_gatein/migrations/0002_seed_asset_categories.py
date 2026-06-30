from django.db import migrations


def seed_categories(apps, schema_editor):
    """Seed initial fixed asset categories."""
    AssetCategory = apps.get_model('fixed_asset_gatein', 'AssetCategory')

    categories = [
        {"category_name": "Machinery", "description": "Production machinery and heavy equipment"},
        {"category_name": "Vehicles", "description": "Forklifts, trucks, and other company vehicles"},
        {"category_name": "Computers/IT", "description": "Computers, laptops, servers, and IT hardware"},
        {"category_name": "Furniture", "description": "Office and factory furniture and fixtures"},
        {"category_name": "Tools/Equipment", "description": "Hand tools, power tools, and equipment"},
        {"category_name": "Electrical", "description": "Electrical assets, motors, panels, and generators"},
        {"category_name": "Lab Equipment", "description": "Laboratory and quality testing equipment"},
        {"category_name": "Other", "description": "Other fixed assets not categorized above"},
    ]

    for cat in categories:
        AssetCategory.objects.get_or_create(
            category_name=cat["category_name"],
            defaults={"description": cat["description"], "is_active": True}
        )


def reverse_seed(apps, schema_editor):
    """Remove seeded categories."""
    AssetCategory = apps.get_model('fixed_asset_gatein', 'AssetCategory')
    category_names = [
        "Machinery", "Vehicles", "Computers/IT", "Furniture",
        "Tools/Equipment", "Electrical", "Lab Equipment", "Other",
    ]
    AssetCategory.objects.filter(category_name__in=category_names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('fixed_asset_gatein', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_categories, reverse_seed),
    ]
