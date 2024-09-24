# Generated by Django 5.0.6 on 2024-07-12 17:07

from __future__ import annotations

import dataclasses
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.db.models import F, Window
from django.db.models.functions import Rank
from sentry_kafka_schemas.schema_types.group_attributes_v1 import GroupAttributesSnapshot

from sentry.issues.attributes import produce_snapshot_to_kafka
from sentry.new_migrations.migrations import CheckedMigration
from sentry.utils import redis
from sentry.utils.iterators import chunked
from sentry.utils.query import RangeQuerySetWrapperWithProgressBarApprox

if TYPE_CHECKING:
    from sentry.models.group import Group
    from sentry.models.groupassignee import GroupAssignee
    from sentry.models.groupowner import GroupOwner

CHUNK_SIZE = 10000


class GroupOwnerType(Enum):
    SUSPECT_COMMIT = 0
    OWNERSHIP_RULE = 1
    CODEOWNERS = 2


@dataclasses.dataclass
class GroupValues:
    id: int
    project_id: int
    status: int
    substatus: int | None
    first_seen: datetime
    num_comments: int
    priority: int | None
    first_release_id: int | None


def _bulk_retrieve_group_values(group_ids: list[int], Group: type[Group]) -> list[GroupValues]:
    group_values_map = {
        group["id"]: group
        for group in Group.objects.filter(id__in=group_ids).values(
            "id",
            "project_id",
            "status",
            "substatus",
            "first_seen",
            "num_comments",
            "priority",
            "first_release",
        )
    }
    assert len(group_values_map) == len(group_ids)

    results = []
    for group_id in group_ids:
        group_values = group_values_map[group_id]
        results.append(
            GroupValues(
                id=group_id,
                project_id=group_values["project_id"],
                status=group_values["status"],
                substatus=group_values["substatus"],
                first_seen=group_values["first_seen"],
                num_comments=group_values["num_comments"] or 0,
                priority=group_values["priority"],
                first_release_id=(group_values["first_release"] or None),
            )
        )
    return results


def _bulk_retrieve_snapshot_values(
    group_values_list: list[GroupValues],
    GroupAssignee: type[GroupAssignee],
    GroupOwner: type[GroupOwner],
) -> list[GroupAttributesSnapshot]:
    group_assignee_map = {
        ga["group_id"]: ga
        for ga in GroupAssignee.objects.filter(
            group_id__in=[gv.id for gv in group_values_list]
        ).values("group_id", "user_id", "team_id")
    }

    group_owner_map = {}

    for group_owner in (
        GroupOwner.objects.annotate(
            position=Window(Rank(), partition_by=[F("group_id"), F("type")], order_by="-date_added")
        )
        .filter(position=1, group_id__in=[g.id for g in group_values_list])
        .values("group_id", "user_id", "team_id", "type")
    ):
        group_owner_map[(group_owner["group_id"], group_owner["type"])] = group_owner

    snapshots = []
    for group_value in group_values_list:
        assignee = group_assignee_map.get(group_value.id)
        suspect_owner = group_owner_map.get((group_value.id, GroupOwnerType.SUSPECT_COMMIT.value))
        ownership_owner = group_owner_map.get((group_value.id, GroupOwnerType.OWNERSHIP_RULE.value))
        codeowners_owner = group_owner_map.get((group_value.id, GroupOwnerType.CODEOWNERS.value))
        snapshot: GroupAttributesSnapshot = {
            "group_deleted": False,
            "project_id": group_value.project_id,
            "group_id": group_value.id,
            "status": group_value.status,
            "substatus": group_value.substatus,
            "priority": group_value.priority,
            "first_release": group_value.first_release_id,
            "first_seen": group_value.first_seen.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "num_comments": group_value.num_comments,
            "timestamp": datetime.now().isoformat(),
            "assignee_user_id": assignee["user_id"] if assignee else None,
            "assignee_team_id": assignee["team_id"] if assignee else None,
            "owner_suspect_commit_user_id": suspect_owner["user_id"] if suspect_owner else None,
            "owner_ownership_rule_user_id": ownership_owner["user_id"] if ownership_owner else None,
            "owner_ownership_rule_team_id": ownership_owner["team_id"] if ownership_owner else None,
            "owner_codeowners_user_id": codeowners_owner["user_id"] if codeowners_owner else None,
            "owner_codeowners_team_id": codeowners_owner["team_id"] if codeowners_owner else None,
        }
        snapshots.append(snapshot)

    return snapshots


def bulk_send_snapshot_values(
    group_ids: list[int],
    Group: type[Group],
    GroupAssignee: type[GroupAssignee],
    GroupOwner: type[GroupOwner],
) -> None:
    group_list = []
    if group_ids:
        group_list.extend(_bulk_retrieve_group_values(group_ids, Group))

    snapshots = _bulk_retrieve_snapshot_values(group_list, GroupAssignee, GroupOwner)

    for snapshot in snapshots:
        produce_snapshot_to_kafka(snapshot)


def backfill_group_attributes_to_snuba(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    Group = apps.get_model("sentry", "Group")
    GroupAssignee = apps.get_model("sentry", "GroupAssignee")
    GroupOwner = apps.get_model("sentry", "GroupOwner")

    backfill_key = "backfill_group_info_to_group_attributes"
    redis_client = redis.redis_clusters.get(settings.SENTRY_MONITORS_REDIS_CLUSTER)

    progress_id = int(redis_client.get(backfill_key) or 0)

    for group_ids in chunked(
        RangeQuerySetWrapperWithProgressBarApprox(
            Group.objects.filter(id__gt=progress_id).values_list("id", flat=True),
            step=CHUNK_SIZE,
            result_value_getter=lambda item: item,
        ),
        CHUNK_SIZE,
    ):
        bulk_send_snapshot_values(group_ids, Group, GroupAssignee, GroupOwner)
        # Save progress to redis in case we have to restart
        redis_client.set(backfill_key, group_ids[-1], ex=60 * 60 * 24 * 7)


class Migration(CheckedMigration):
    # This flag is used to mark that a migration shouldn't be automatically run in production.
    # This should only be used for operations where it's safe to run the migration after your
    # code has deployed. So this should not be used for most operations that alter the schema
    # of a table.
    # Here are some things that make sense to mark as post deployment:
    # - Large data migrations. Typically we want these to be run manually so that they can be
    #   monitored and not block the deploy for a long period of time while they run.
    # - Adding indexes to large tables. Since this can take a long time, we'd generally prefer to
    #   run this outside deployments so that we don't block them. Note that while adding an index
    #   is a schema change, it's completely safe to run the operation after the code has deployed.
    # Once deployed, run these manually via: https://develop.sentry.dev/database-migrations/#migration-deployment

    is_post_deployment = True

    dependencies = [
        ("sentry", "0738_rm_reprocessing_step3"),
    ]

    operations = [
        migrations.RunPython(
            backfill_group_attributes_to_snuba,
            reverse_code=migrations.RunPython.noop,
            hints={"tables": ["sentry_groupedmessage"]},
        )
    ]
