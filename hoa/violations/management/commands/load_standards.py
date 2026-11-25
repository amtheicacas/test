import json
from pathlib import Path

from django.core.management.base import BaseCommand

from violations.models import ViolationType

DATA = Path(__file__).resolve().parents[2] / "data"


class Command(BaseCommand):
    help = ("Load/refresh the violation list: the Community Standards (categories, rule text, drafted observation and cure) and the "
            "Declaration (CC&Rs) restrictions. Safe to re-run.")

    def handle(self, *a, **kw):
        for fname in ("standards_catalog.json", "declaration_catalog.json"):
            f = DATA / fname
            if not f.exists():
                continue
            data = json.loads(f.read_text(encoding="utf8"))
            made = updated = 0
            for i in data["items"]:
                declaration = i.get("source") == "Declaration"
                ref = (f"Declaration \u00a7{i['section']} {i['standards_title']}" if declaration
                       else f"Community Standards \u00a7{i['section']} {i['standards_title']}")
                _, created = ViolationType.objects.update_or_create(code=i["code"], defaults=dict(
                    name=i["title"], reference=ref, section=i["section"], standards_title=i["standards_title"],
                    source="Declaration" if declaration else "Community Standards",
                    category=i["category"], rule_text=i["rule"], observed_template=i["observed"],
                    cure_template=i["cure"], keywords=i["keywords"], default_cure_days=i["cure_days"]))
                made += created
                updated += not created
            self.stdout.write(f"{made} created, {updated} updated ({data['source']})")
