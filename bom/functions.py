"""Helpers for the part-class approval workflow.

Ported from production bfc72fa. Production's version reproduced three silent failures that
between them are why the subsystem went unused for ~21 months (59 of 60 instances stuck in the
first state). Each fix is marked CORRECTION and explained where it sits. See
CHIT-001-indabom-port-plan.md, "PHASE D — RE-SCOPED".
"""

import logging

from django.contrib import messages
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import strip_tags

import bom.state_diagram_builder as diagrams
from bom import constants
from bom.forms import (
    ChangeStateAssignedUsersForm,
    CreatePartClassWorkflowTransitionForm,
    PartClassWorkflowStateChangeForm,
)
from bom.models import (
    PartClassWorkflow,
    PartClassWorkflowCompletedTransition,
    PartClassWorkflowState,
    PartClassWorkflowStateTransition,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workflow definition — validation and persistence
# ---------------------------------------------------------------------------

def validate_transition_forms(request, workflow_form, organization):
    result = {
        'has_final_state': False,
        'has_initial_state': False,
        'valid_transitions': [],
    }

    initial_state_id = workflow_form.data.get('initial_state')
    initial_state = None
    if initial_state_id:
        initial_state = PartClassWorkflowState.objects.available_to(organization).filter(
            id=initial_state_id).first()

    for i in range(constants.NUMBER_WORKFLOW_TRANSITIONS_MAX):
        transition_form = CreatePartClassWorkflowTransitionForm(
            request.POST, prefix="trans{}".format(i), organization=organization)
        if transition_form.is_valid():
            result['valid_transitions'].append(transition_form.cleaned_data)
            if transition_form.cleaned_data['target_state'].is_final_state:
                result['has_final_state'] = True
            if transition_form.cleaned_data['source_state'] == initial_state:
                result['has_initial_state'] = True

    return result


def get_transitions_error(valid_transition_results):
    if len(valid_transition_results['valid_transitions']) == 0:
        return "You cannot create a workflow without defining any state transitions."
    if not valid_transition_results['has_final_state']:
        return "Must be able to transition to a final state."
    if not valid_transition_results['has_initial_state']:
        return "Transitions must contain the workflow's initial state."
    return False


def create_transitions(transitions, workflow):
    """Persist each forward transition and its matching backward transition.

    CORRECTION 1: production wrapped the body in a bare `except: pass`. Two consequences.
    A duplicate transition raised IntegrityError from the unique_together and was swallowed --
    but inside ATOMIC_REQUESTS the broken transaction then rejected every subsequent save, which
    was swallowed too. A workflow could be created with most of its transitions silently missing
    and no error anywhere. get_or_create makes the duplicate case a no-op instead of an error,
    and anything genuinely wrong now surfaces.
    """
    for transition in transitions:
        source_state = transition['source_state']
        target_state = transition['target_state']

        PartClassWorkflowStateTransition.objects.get_or_create(
            workflow=workflow, source_state=source_state, target_state=target_state,
            defaults={'direction_in_workflow': 'forward'},
        )
        PartClassWorkflowStateTransition.objects.get_or_create(
            workflow=workflow, source_state=target_state, target_state=source_state,
            defaults={'direction_in_workflow': 'backward'},
        )


def validate_new_workflow(request, workflow_form, organization):
    valid_results = {'is_valid': True}

    if not workflow_form.is_valid():
        valid_results['is_valid'] = False
        valid_results['error_msg'] = "Invalid entries for workflow"
        return valid_results

    valid_transition_results = validate_transition_forms(request, workflow_form, organization)
    transitions_error_msg = get_transitions_error(valid_transition_results)
    if transitions_error_msg:
        valid_results['is_valid'] = False
        valid_results['error_msg'] = transitions_error_msg

    valid_results['valid_transitions'] = valid_transition_results['valid_transitions']
    return valid_results


def validate_new_workflow_state(workflow_state_form):
    valid_results = {'is_valid': True}

    if not workflow_state_form.is_valid():
        valid_results['is_valid'] = False
        errors = ', '.join(workflow_state_form.errors)
        valid_results['error_msg'] = f'Error creating new state. Check these fields: {errors}'

    return valid_results


def edit_existing_workflow(request, form, organization):
    workflow_id = request.POST.get('editing_existing_workflow')
    existing_workflow = PartClassWorkflow.objects.available_to(organization).filter(
        id=workflow_id).first()

    # CORRECTION 2: production dereferenced this without checking. A stale or forged
    # editing_existing_workflow value produced AttributeError on None rather than a message.
    if existing_workflow is None:
        messages.error(request, 'That workflow no longer exists.')
        return False

    if not form.data.get('name'):
        messages.error(request, 'The workflow name cannot be blank.')
        return False

    if not form.data.get('initial_state'):
        messages.error(request, 'An initial state must be selected.')
        return False

    initial_state = PartClassWorkflowState.objects.available_to(organization).filter(
        id=form.data['initial_state']).first()
    if initial_state is None:
        messages.error(request, 'That initial state no longer exists.')
        return False

    valid_transition_results = validate_transition_forms(request, form, organization)
    transitions_error_msg = get_transitions_error(valid_transition_results)
    if transitions_error_msg:
        messages.error(request, transitions_error_msg)
        return False

    # CORRECTION 3: the delete-then-recreate below is now atomic. Production deleted every
    # transition first and recreated them through the swallow-everything loop above, so a
    # failure mid-rebuild left the workflow with no transitions at all and said nothing.
    with transaction.atomic():
        existing_workflow.name = form.data['name']
        existing_workflow.initial_state = initial_state
        existing_workflow.description = form.data.get('description', '')
        existing_workflow.save()

        PartClassWorkflowStateTransition.objects.filter(workflow=existing_workflow).delete()
        create_transitions(valid_transition_results['valid_transitions'], existing_workflow)

    return True


# ---------------------------------------------------------------------------
# Assignment and notification
# ---------------------------------------------------------------------------

def assign_state_users(workflow_instance, state=None):
    """Point the instance's assignee list at the given state's configured assignees.

    CORRECTION 4 (D-a, root cause 1 of non-adoption): production created a PartWorkflowInstance
    with part/workflow/current_state and never populated currently_assigned_users. On the
    production snapshot only 2 of 60 instances had any assignee, so 59 parts sat in a state that
    DID have an owner configured while appearing in nobody's queue. Every place that sets or
    moves current_state now goes through here.
    """
    state = state or workflow_instance.current_state
    if state is None:
        workflow_instance.currently_assigned_users.clear()
        return []

    users = list(state.assigned_users.all())
    workflow_instance.currently_assigned_users.set(users)
    return users


def send_new_task_email(message_context, request=None):
    """Notify one assignee. Returns True on success.

    CORRECTION 5 (D-b, root cause 2 of non-adoption): production passed fail_silently=True with
    no EMAIL_* setting defined on either side, so from_email was '', Django substituted
    'webmaster@localhost', the container ran no MTA, and the refused connection was swallowed.
    No mail, no exception, no log line, for three years. Failures are now logged at ERROR and
    surfaced in-app to the person whose action triggered them.
    """
    recipient = message_context['assigned_user']
    if not getattr(recipient, 'email', ''):
        logger.error("Workflow notification skipped: user %s has no email address", recipient)
        if request is not None:
            messages.warning(request, f"{recipient} has no email address on file and was not notified.")
        return False

    html_message = render_to_string('bom/workflow_email_template.html', message_context)
    plain_message = strip_tags(html_message)

    try:
        send_mail(
            subject=f"[IndaBOM] New Task For Part {message_context['part']}!",
            message=plain_message,
            recipient_list=[recipient.email],
            html_message=html_message,
            from_email=None,   # falls through to DEFAULT_FROM_EMAIL
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("Workflow notification to %s failed", recipient.email)
        if request is not None:
            messages.warning(
                request,
                f"Could not email {recipient.email}. The workflow was updated; the notification was not sent.")
        return False


def notify_users(users, request, part, transition_name, comments, sender_name=None):
    """Send one new-task notification per user. Returns the count sent."""
    host = request.get_host() if request is not None else ''
    scheme = 'https' if (request is not None and request.is_secure()) else 'http'
    part_info_url = f'{scheme}://{host}' + reverse('bom:part-info', kwargs={'part_id': part.id}) + '#workflow'

    sent = 0
    for user in users:
        sent += bool(send_new_task_email(
            message_context={
                'assigned_user': user,
                'part': part,
                'previous_assigned_user': sender_name or '',
                'comments': comments,
                'transition_name': transition_name,
                'part_info_url': part_info_url,
            },
            request=request,
        ))
    return sent


# ---------------------------------------------------------------------------
# Part-info workflow panel
# ---------------------------------------------------------------------------

def get_part_workflow_context(request, workflow_instance):
    context = {}
    context['all_assigned_users'] = workflow_instance.currently_assigned_users.all()
    if len(context['all_assigned_users']) == 0:
        context['all_assigned_users'] = workflow_instance.current_state.assigned_users.all()

    context['is_assigned_user'] = request.user in context['all_assigned_users']

    all_forward_transitions = PartClassWorkflowStateTransition.objects.filter(
        workflow=workflow_instance.workflow,
        direction_in_workflow='forward',
    )

    current_state_name = workflow_instance.current_state.name if workflow_instance.current_state else None
    context['workflow_str_lines'] = [
        {'line': row['line'], 'is_current': row['name'] == current_state_name}
        for row in diagrams.workflow_rows(
            initial_state=workflow_instance.workflow.initial_state,
            forward_transitions=all_forward_transitions,
        )
    ]

    context['current_forward_transitions'] = all_forward_transitions.filter(
        source_state=workflow_instance.current_state,
    )

    context['current_backward_transitions'] = PartClassWorkflowStateTransition.objects.filter(
        workflow=workflow_instance.workflow,
        source_state=workflow_instance.current_state,
        direction_in_workflow='backward',
    )

    if workflow_instance.current_state.is_final_state:
        context['submit_state_form'] = PartClassWorkflowStateChangeForm(final_transition=True)
    else:
        context['submit_state_form'] = PartClassWorkflowStateChangeForm(
            forward_transitions=context['current_forward_transitions'])

    if context['current_backward_transitions']:
        context['reject_state_form'] = PartClassWorkflowStateChangeForm(
            backward_transitions=context['current_backward_transitions'])

    profile = request.user.bom_profile()
    if request.user.is_superuser or profile.role == constants.ROLE_TYPE_ADMIN or context['is_assigned_user']:
        context['change_assigned_users_form'] = ChangeStateAssignedUsersForm(
            organization=profile.organization,
            initial={'assigned_users': context['all_assigned_users']},
        )
        context['change_state_form_action'] = reverse(
            'bom:part-info', kwargs={'part_id': workflow_instance.part.id})

    return context


# ---------------------------------------------------------------------------
# Part-info workflow actions
# ---------------------------------------------------------------------------

def _part_info_redirect(part, anchor='#workflow'):
    return HttpResponseRedirect(
        reverse('bom:part-info', kwargs={'part_id': part.id}) + anchor)


def change_assigned_users_and_refresh(request, workflow_instance):
    organization = request.user.bom_profile().organization
    change_assigned_users_form = ChangeStateAssignedUsersForm(request.POST, organization=organization)
    if not change_assigned_users_form.is_valid():
        return HttpResponse("Error: " + str(change_assigned_users_form.errors))

    new_assigned_users = change_assigned_users_form.cleaned_data['assigned_users']
    workflow_instance.currently_assigned_users.set(new_assigned_users)

    if change_assigned_users_form.cleaned_data['notify_new_users']:
        notify_users(
            users=workflow_instance.currently_assigned_users.all(),
            request=request,
            part=workflow_instance.part,
            transition_name=workflow_instance.current_state.name,
            comments=change_assigned_users_form.cleaned_data['comments'],
            sender_name=request.user.get_full_name(),
        )

    return _part_info_redirect(workflow_instance.part)


def change_workflow_state_and_refresh(request, workflow_instance):
    part = workflow_instance.part
    change_state_form = PartClassWorkflowStateChangeForm(request.POST)

    # CORRECTION 6: production called messages.error here and then FELL THROUGH into
    # cleaned_data, which on an invalid form need not contain the keys it reads.
    if not change_state_form.is_valid():
        messages.error(request, f"An error occurred: {change_state_form.errors}")
        return _part_info_redirect(part)

    selected_transition = change_state_form.cleaned_data['transition']
    comments = change_state_form.cleaned_data['comments']
    is_final = workflow_instance.current_state.is_final_state

    if selected_transition is None and not is_final:
        messages.error(request, "Error, please select a transition")
        return _part_info_redirect(part)

    with transaction.atomic():
        PartClassWorkflowCompletedTransition.objects.create(
            transition=selected_transition,
            completed_by=request.user,
            comments=comments,
            part=part,
        )

        if is_final and 'submit-workflow-state' in request.POST:
            workflow_instance.delete()
            # CORRECTION 7: production read workflow_instance.part AFTER delete() to build this
            # message. The part is captured above instead.
            messages.success(request, f"Workflow for {part} completed!")
            return _part_info_redirect(part, anchor='')

        workflow_instance.current_state = selected_transition.target_state
        workflow_instance.save()
        next_users = assign_state_users(workflow_instance, selected_transition.target_state)

    # CORRECTION 8: production notified selected_transition.SOURCE_state.assigned_users -- the
    # people who had just finished the step -- while labelling the mail with the TARGET state's
    # name and assigning the target's users to the instance. Had email ever worked, it would have
    # told the wrong people they had a new task. Notify the users now actually assigned.
    if change_state_form.cleaned_data['notifying_next_users'] and next_users:
        notify_users(
            users=next_users,
            request=request,
            part=part,
            transition_name=selected_transition.target_state.name,
            comments=comments,
            sender_name=request.user.get_full_name(),
        )

    return _part_info_redirect(part)
