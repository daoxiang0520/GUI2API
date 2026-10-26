"""One recovery vocabulary for CLI, MCP and DSH responses."""

NEXT = {
    'stale_resource':'list_resources_again','resource_changed':'list_resources_again',
    'resource_unavailable':'inspect_resource_availability','resource_source_unavailable':'use_explicit_rendered_or_resource_provider',
    'resource_scheme_unavailable':'use_a_read_only_resource_provider','stream_unavailable':'use_an_explicit_stream_provider',
    'representation_unavailable':'choose_an_advertised_representation','resource_capture_unavailable':'use_a_verified_capture_provider',
    'resource_not_visible':'show_the_resource_then_read','resource_too_large':'raise_explicit_budget_or_use_a_smaller_variant',
    'resource_storage_limit':'release_owned_artifacts','resource_timeout':'inspect_availability_without_replaying_actions',
    'stale_ref':'find_or_rebind', 'stale_capability':'rediscover', 'stale_catalog':'rediscover',
    'stale_cursor':'rescan', 'stale_snapshot':'rescan', 'unknown_source':'attach',
    'target_occluded':'bring_to_front_or_dismiss_popup', 'target_unavailable':'restore_then_find',
    'focus_unverified':'bring_to_front_then_find', 'disabled_control':'observe_enabled_state',
    'context_changed':'reobserve_context', 'value_conflict':'read_current_value',
    'postcondition_failed':'observe_actual_state_do_not_replay', 'outcome_unknown':'observe_actual_state_do_not_replay',
    'verification_failed':'read_actual_state_do_not_replay', 'invoke_target_mismatch':'observe_actual_state_do_not_replay',
    'cdp_not_discovered':'use_existing_endpoint_or_native_discovery', 'cdp_probe_incomplete':'use_explicit_endpoint_or_native_discovery',
    'ambiguous_target':'choose_candidate', 'ambiguous_binding':'choose_candidate',
    'invalid_json':'send_valid_json','invalid_request':'read_schema',
    'element_not_found':'observe_or_navigate', 'invalid_arguments':'read_schema',
    'protected_control':'choose_unprotected_control', 'desktop_not_interactive':'restore_interactive_desktop',
    'unreliable_invoke':'use_click_input_or_postcondition', 'console_unavailable':'request_accessibility_then_use_TextPattern_or_terminal_integration',
}


def decorate(response):
    if not response.get('ok'):
        error=response.get('error',{})
        action=NEXT.get(error.get('code'),'inspect_error_and_schema')
        response={**response,'error':{**error,'next_action':action},'next_action':action}
    return response
