import uuid
from dataclasses import dataclass
from typing import Any

from azure.ai.ml import MLClient
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters


@dataclass(frozen=True)
class RequiredAssignment:
    principal_name: str
    principal_id: str
    role_name: str
    scope: str


def _role_definition_id(
    authorization_client: AuthorizationManagementClient,
    scope: str,
    role_name: str,
) -> str:
    role = next(
        iter(
            authorization_client.role_definitions.list(
                scope,
                filter=f"roleName eq '{role_name}'",
            )
        ),
        None,
    )
    if not role or not role.id:
        raise RuntimeError(f"Role definition was not found: {role_name}")
    return role.id


def _ensure_assignment(
    authorization_client: AuthorizationManagementClient,
    assignment: RequiredAssignment,
) -> None:
    role_definition_id = _role_definition_id(
        authorization_client,
        assignment.scope,
        assignment.role_name,
    )
    assignment_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            assignment.scope + assignment.principal_id + role_definition_id,
        )
    )
    try:
        authorization_client.role_assignments.get(
            assignment.scope,
            assignment_id,
        )
        print(
            f"  exists: {assignment.principal_name} -> "
            f"{assignment.role_name} @ {assignment.scope.rsplit('/', 1)[-1]}"
        )
        return
    except Exception as error:
        if "RoleAssignmentNotFound" not in str(error) and "could not be found" not in str(error):
            raise

    try:
        authorization_client.role_assignments.create(
            assignment.scope,
            assignment_id,
            RoleAssignmentCreateParameters(
                role_definition_id=role_definition_id,
                principal_id=assignment.principal_id,
                principal_type="ServicePrincipal",
            ),
        )
        action = "created"
    except Exception as error:
        if "RoleAssignmentExists" not in str(error):
            raise
        action = "exists"
    print(
        f"  {action}: {assignment.principal_name} -> "
        f"{assignment.role_name} @ {assignment.scope.rsplit('/', 1)[-1]}"
    )


def configure_permissions(
    settings: Any,
    ml_client: MLClient,
    authorization_client: AuthorizationManagementClient,
) -> list[RequiredAssignment]:
    compute = ml_client.compute.get(settings.compute_name)
    workspace = ml_client.workspaces.get(settings.aml_workspace)
    if not compute.identity or not compute.identity.principal_id:
        raise RuntimeError(f"Compute {settings.compute_name} has no system-assigned identity")
    if not workspace.identity or not workspace.identity.principal_id:
        raise RuntimeError(f"Workspace {settings.aml_workspace} has no system-assigned identity")

    storage_scope = workspace.storage_account
    assignments = [
        RequiredAssignment(
            principal_name=f"compute:{compute.name}",
            principal_id=compute.identity.principal_id,
            role_name="Cognitive Services OpenAI User",
            scope=settings.foundry_scope,
        ),
        RequiredAssignment(
            principal_name=f"compute:{compute.name}",
            principal_id=compute.identity.principal_id,
            role_name="Storage Blob Data Contributor",
            scope=storage_scope,
        ),
        RequiredAssignment(
            principal_name=f"workspace:{workspace.name}",
            principal_id=workspace.identity.principal_id,
            role_name="Storage Blob Data Contributor",
            scope=storage_scope,
        ),
        RequiredAssignment(
            principal_name=f"workspace:{workspace.name}",
            principal_id=workspace.identity.principal_id,
            role_name="Storage File Data Privileged Contributor",
            scope=storage_scope,
        ),
    ]

    print("Ensuring runtime role assignments:")
    for assignment in assignments:
        _ensure_assignment(authorization_client, assignment)

    for assignment in assignments:
        expected_role_id = _role_definition_id(
            authorization_client,
            assignment.scope,
            assignment.role_name,
        )
        matches = [
            existing
            for existing in authorization_client.role_assignments.list_for_scope(
                assignment.scope
            )
            if existing.principal_id == assignment.principal_id
            and existing.role_definition_id
            and existing.role_definition_id.casefold() == expected_role_id.casefold()
        ]
        if not matches:
            raise RuntimeError(
                f"Role assignment did not verify: {assignment.principal_name} -> "
                f"{assignment.role_name}"
            )

    print(f"Verified {len(assignments)} required role assignments")
    return assignments