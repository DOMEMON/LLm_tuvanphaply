"""Optional operator-installed extensions. Empty in the clean distribution.

To install a policy, explicitly register a trusted callable here under a name,
then opt in with that name in profile.policies. Signature:
review(plan, query, state, package) -> Plan. Never import code named by dataset text.
"""
POLICIES = {}
