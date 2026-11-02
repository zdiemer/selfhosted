"""Shared routing for activation and subsequent dashboard upgrades."""

# Keep informational conditions visible in Grafana without paging the owner.
DASHBOARD_ONLY = {'sh-arr-health', 'sh-crowdsec-spike'}


def notification_settings(rule, config):
    if not config['enabled'] or rule['uid'] in DASHBOARD_ONLY | {'sh-sms-gateway-down'}:
        return {'receiver': 'unwired'}
    settings = {k: config[k] for k in
                ('receiver', 'group_wait', 'group_interval', 'repeat_interval')}
    if rule['uid'] == 'sh-ntfy-unreachable':
        settings['receiver'] = 'notification-path-email'
        return settings
    settings['group_by'] = ['grafana_folder', 'alertname']
    return settings
