"""Finish a reviewed consultation, recovering informational failures locally.

No sending, model calls or account operations. The same content gate is applied
to the original and to a new deterministic continuation. Real action/intake
requirements cannot be converted to a clarification by this boundary.
"""


def finish(answer,facts,question=''):
    from .reviewed_workflow import attach_reviewed_workflow
    from .auto_policy import block_reason,owner_reason
    from .autonomous_fallback import reply
    from .privacy import scrub
    answer=attach_reviewed_workflow(answer,facts)
    current=(facts.get('conversation_turn',{}).get('current_text')
             or facts.get('conversation',{}).get('latest_message') or question
             or facts.get('conversation',{}).get('latest_client_message',''))
    reason=block_reason(answer,current_text=current)
    if reason is None:return answer
    if not isinstance(answer,dict):return answer
    evidence=answer.get('evidence',{})
    if not isinstance(evidence,dict):evidence={}
    if (evidence.get('owner_action_required') is True or evidence.get('manual_secret_intake')
            or evidence.get('review_required') is True
            or evidence.get('new_account_stage') in ('owner_registration','owner_session_intake','owner_details_received')):
        return answer
    # The gate's presentation reason can be generic ("saved manual reason").
    # Preserve the actual safe cause too: otherwise a policy conflict or an
    # explicit human/intake requirement could be mistaken for a source gap.
    # Never use the rejected response text to construct this reason.
    details=[reason]+[evidence[key] for key in ('handoff_reason','diagnostic')
                      if isinstance(evidence.get(key),str) and evidence[key]]
    recovery_reason='\n'.join(scrub(detail)[:1400] for detail in details)
    replacement=reply(facts,recovery_reason,question)
    if replacement is None or block_reason(replacement,current_text=current) is not None:
        return answer
    replacement.setdefault('evidence',{})['recovered_content_gate']=owner_reason(reason)
    return replacement
