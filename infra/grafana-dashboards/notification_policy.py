"""Shared routing for activation and subsequent dashboard upgrades."""


def notification_settings(rule, config):
    if not config['enabled'] or rule['uid'] == 'sh-sms-gateway-down':
        return {'receiver': 'unwired'}
    settings = {k: config[k] for k in
                ('receiver', 'group_wait', 'group_interval', 'repeat_interval')}
    if rule['uid'] == 'sh-ntfy-unreachable':
        settings['receiver'] = 'notification-path-email'
        return settings
    return settings
