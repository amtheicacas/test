import json
from pathlib import Path

from django.core.management.base import BaseCommand

from violations.models import ViolationType

CATALOG = Path(__file__).resolve().parents[2] / "data" / "standards_catalog.json"


class Command(BaseCommand):
    help = "Load/refresh the Community Standards violation list (categories, rule text, drafted observation and cure). Safe to re-run."

    def handle(self, *a, **kw):
        data = json.loads(CATALOG.read_text(encoding="utf8"))
        made = updated = 0
        for i in data["items"]:
            ref = f"Community Standards §{i['section']} {i['standards_title']}"
            _, created = ViolationType.objects.update_or_create(code=i["code"], defaults=dict(
                name=i["title"], reference=ref, section=i["section"], standards_title=i["standards_title"],
                category=i["category"], rule_text=i["rule"], observed_template=i["observed"],
                cure_template=i["cure"], keywords=i["keywords"], default_cure_days=i["cure_days"]))
            made += created
            updated += not created
        self.stdout.write(f"{made} created, {updated} updated ({data['source']})")
