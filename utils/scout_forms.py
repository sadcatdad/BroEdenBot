"""Dashboard-owned Scout form definitions shared by Discord and The Garden."""
import copy


def field(key, label, placeholder='', required=False, enabled=True, locked=False):
    return dict(key=key,label=label,placeholder=placeholder,required=required,enabled=enabled,locked=locked)

FORM_DEFAULTS = {
    'activity': [field('summary','What happened?','Briefly describe the situation.',True,locked=True),
                 field('action','What did you do?','Describe your response or action.',True,locked=True),
                 field('outcome','How did it end?','What changed, or what is still unresolved?'),
                 field('evidence','Message link or supporting details','Paste a message/screenshot link, or leave blank.')],
    'escalation': [field('reason','What help do you need?','Explain the concern and the help needed from higher staff.',True,locked=True)],
    'report': [field('explanation','Why are you reporting this message?','Explain your concern. Only authorized dashboard readers can see this.',True,locked=True)],
    'outcome': [field('reason','How was this report handled?','Describe the outcome before marking it handled.',True,locked=True)],
}
for profile, fields in FORM_DEFAULTS.items():
    fields.extend(field(f'custom_{i+1}','','',False,False) for i in range(5-len(fields)))


def validate_forms(value):
    if not isinstance(value,dict) or set(value)-set(FORM_DEFAULTS):
        raise ValueError('Choose valid Scout forms.')
    result=copy.deepcopy(FORM_DEFAULTS)
    for profile, defaults in FORM_DEFAULTS.items():
        supplied=value.get(profile,defaults)
        if not isinstance(supplied,list) or len(supplied)!=len(defaults):
            raise ValueError('Each Discord form supports at most five questions.')
        for index, (original, item) in enumerate(zip(defaults,supplied)):
            if not isinstance(item,dict) or item.get('key')!=original['key']:
                raise ValueError('Form question identifiers cannot be changed.')
            clean=dict(original)
            clean.update(label=str(item.get('label','')).strip(),placeholder=str(item.get('placeholder','')).strip(),
                         enabled=bool(item.get('enabled')),required=bool(item.get('required')))
            if original['locked']:
                clean.update(enabled=True,required=True)
            if len(clean['label'])>45 or len(clean['placeholder'])>100 or (clean['enabled'] and not clean['label']):
                raise ValueError('Enabled questions need a label (up to 45 characters); hints allow up to 100 characters.')
            result[profile][index]=clean
    return result


def answers_for(fields, values):
    """Validate configured requirements; snapshot custom labels with their answers."""
    answers=[]
    for f in fields:
        if not f['enabled']:
            continue
        value=str(values.get(f['key']) or '').strip()
        if f['required'] and not value:
            raise ValueError(f"{f['label']} is required.")
        limit=2000 if f['key']=='evidence' else 4000
        if len(value)>limit:
            raise ValueError(f"{f['label']} must be at most {limit:,} characters.")
        if f['key'].startswith('custom_') and value:
            answers.append(dict(key=f['key'],label=f['label'],answer=value))
    return answers
