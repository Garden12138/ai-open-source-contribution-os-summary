import { api, element } from './api.js';

const permissionLabels = {
  allow_once: '允许',
  reject_once: '拒绝',
  reject_always: '拒绝',
  allow_always: '本任务内总是允许',
};

function permissionFields(node, { tool_call: tool = {}, options = [] }, send) {
  node.append(element('p', '', [tool.kind, tool.title].filter(Boolean).join(' · ') || 'Agent 工具'));
  const command = tool.rawInput?.command ?? tool.rawInput?.cmd;
  if (command) node.append(element('pre', '', Array.isArray(command) ? command.join(' ') : command));
  const paths = new Set([
    ...(tool.locations || []).map(location => location.path),
    ...(tool.content || []).map(item => item.path),
    tool.rawInput?.path,
  ].filter(Boolean));
  for (const path of paths) node.append(element('p', 'request-path', path));
  const actions = element('div', 'request-actions');
  for (const option of options) {
    if (option.kind === 'reject_always' && options.some(choice => choice.kind === 'reject_once')) continue;
    if (option.kind === 'allow_always' &&
      (!(tool.rawInput || tool.locations?.length) || JSON.stringify(tool).includes('[REDACTED]') ||
        !options.some(choice => choice.kind === 'allow_once'))) continue;
    const button = element('button', '', permissionLabels[option.kind] || option.name || option.optionId);
    button.type = 'button';
    button.addEventListener('click', () => send({ option_id: option.optionId }));
    actions.append(button);
  }
  node.append(actions);
}

function choices(spec) {
  return (spec.oneOf || spec.anyOf)?.map(option => [option.const, option.title || option.const]) ||
    spec.enum?.map(value => [value, value]);
}

function questionField(spec, required) {
  const options = choices(spec.type === 'array' ? spec.items || {} : spec);
  if (options || spec.type === 'boolean') {
    const select = element('select');
    select.multiple = spec.type === 'array';
    const values = options || [[true, '是'], [false, '否']];
    if (!select.multiple) select.append(new Option('请选择', ''));
    for (const [index, [value, title]] of values.entries()) {
      const option = new Option(title, String(index));
      option.selected = Array.isArray(spec.default) ? spec.default.includes(value) : value === spec.default;
      select.append(option);
    }
    select.required = required;
    return [select, () => select.multiple
      ? [...select.selectedOptions].map(option => values[Number(option.value)][0])
      : (select.value === '' ? undefined : values[Number(select.value)][0])];
  }
  const input = element('input');
  const numeric = spec.type === 'integer' || spec.type === 'number';
  input.type = numeric ? 'number' : 'text';
  input.required = required;
  if (spec.default != null) input.value = spec.default;
  if (numeric) {
    input.step = spec.type === 'integer' ? '1' : 'any';
    if (spec.minimum != null) input.min = spec.minimum;
    if (spec.maximum != null) input.max = spec.maximum;
  } else {
    if (spec.minLength != null) input.minLength = spec.minLength;
    if (spec.maxLength != null) input.maxLength = spec.maxLength;
  }
  return [input, () => {
    if (!input.value) return undefined;
    if (numeric) return input.valueAsNumber;
    return spec.type === 'array' ? input.value.split(',').map(value => value.trim()) : input.value;
  }];
}

function questionFields(node, { message, schema }, send) {
  if (message) node.append(element('p', '', message));
  const fields = [];
  for (const [key, spec] of Object.entries(schema?.properties || {})) {
    const [control, read] = questionField(spec, schema.required?.includes(key));
    const label = element('label', 'question-field');
    label.append(element('span', '', spec.title || key), control);
    if (spec.description) label.append(element('small', 'muted', spec.description));
    node.append(label);
    fields.push([key, read]);
  }
  const submit = element('button', 'primary', '提交回答');
  submit.type = 'submit';
  node.append(submit);
  node.parentElement.addEventListener('submit', event => {
    event.preventDefault();
    const answers = Object.fromEntries(fields.map(([key, read]) => [key, read()]).filter(([, value]) => value !== undefined));
    send({ answers });
  });
}

export function requestCard(event, path) {
  const { type, payload } = event;
  const permission = type === 'permission.requested';
  const card = element('form', 'activity request-card');
  const status = element('p', 'request-status muted');
  status.setAttribute('role', 'status');
  const fields = element('fieldset');
  fields.append(element('legend', '', permission ? '权限请求' : 'Agent 提问'));
  card.append(fields, status);

  function disable(message) {
    fields.disabled = true;
    card.classList.add('request-closed');
    status.textContent = message;
  }

  async function send(body) {
    if (fields.disabled) return;
    fields.disabled = true;
    status.textContent = '正在提交…';
    try {
      await api(`${path}/requests/${encodeURIComponent(payload.request_id)}/respond`, { method: 'POST', body });
      disable(permission ? '已响应' : '已回答');
    } catch (error) {
      if (error.code === 'request_expired') {
        disable('已失效');
      } else {
        fields.disabled = false;
        status.textContent = error.message;
      }
    }
  }

  if (permission) {
    card.addEventListener('submit', event => event.preventDefault());
    permissionFields(fields, payload, send);
  } else {
    questionFields(fields, payload, send);
  }
  if (payload.resolution) {
    if (payload.resolution === 'system.request_expired' || payload.cancelled) disable('已失效');
    else disable(permission ? '已响应' : '已回答');
  }
  return card;
}
