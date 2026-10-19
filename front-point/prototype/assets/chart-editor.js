(() => {
  const triggers = document.querySelectorAll('[data-chart-editor]');
  if (!triggers.length) return;

  const dialog = document.createElement('dialog');
  dialog.className = 'chart-editor';
  dialog.setAttribute('aria-labelledby', 'chart-editor-title');
  dialog.innerHTML = `
    <div class="chart-editor-head">
      <div><span class="label">图表 AI 编辑</span><h2 id="chart-editor-title" tabindex="-1"></h2></div>
      <button class="chart-editor-close" type="button" aria-label="关闭编辑抽屉">×</button>
    </div>
    <div class="chart-editor-body">
      <p class="chart-editor-intro">输入修改要求后，系统将基于当前图表生成一个新版本，并保留旧版本供选择。</p>
      <div class="chart-editor-state"><strong>当前无可编辑图表</strong><p>取得并核对专利数据、生成图表后，才能使用 AI 编辑。</p></div>
      <fieldset class="chart-editor-modes">
        <legend>编辑类型</legend>
        <div class="chart-editor-options">
          <label><input type="radio" name="chart-edit-type" value="style" checked><span>样式调整</span></label>
          <label><input type="radio" name="chart-edit-type" value="data"><span>数据调整</span></label>
          <label><input type="radio" name="chart-edit-type" value="insight"><span>图表解读</span></label>
        </div>
      </fieldset>
      <div class="field"><label for="chart-editor-prompt">编辑指令</label><textarea id="chart-editor-prompt" rows="4" placeholder="描述希望调整的内容"></textarea></div>
      <div class="chart-editor-scope"><strong>影响范围</strong><p id="chart-editor-scope-text"></p></div>
      <div class="chart-editor-versions">
        <div class="section-head"><h3>版本记录</h3><span class="status-chip">暂无版本</span></div>
        <div class="field"><label for="chart-editor-version">选择版本</label><select id="chart-editor-version" disabled><option>暂无版本可选</option></select></div>
        <p>生成新版本后，可切换查看和恢复旧版；原始专利数据与旧版均保留。</p>
      </div>
    </div>
    <div class="chart-editor-foot"><button class="btn primary" type="button" disabled>生成新版本</button><p>尚未接入专利数据与 AI 服务，暂不可生成。</p></div>
  `;
  document.body.append(dialog);

  const title = dialog.querySelector('#chart-editor-title');
  const scope = dialog.querySelector('#chart-editor-scope-text');
  const closeButton = dialog.querySelector('.chart-editor-close');
  const prompt = dialog.querySelector('#chart-editor-prompt');
  const scopes = {
    style: '仅改变当前图表的呈现方式；不修改专利数据、统计口径或结论。新版本保留旧版。',
    data: '调整筛选条件或统计口径后，必须基于原始专利数据重新计算；原始数据和旧版不覆盖。',
    insight: '图表解读必须依据已有专利数据、统计口径和可回查的证据，不凭空补写结论。'
  };
  let opener = null;
  scope.textContent = scopes.style;

  triggers.forEach((trigger) => trigger.addEventListener('click', () => {
    opener = trigger;
    title.textContent = trigger.dataset.chartTitle || '当前图表';
    dialog.querySelector('input[value="style"]').checked = true;
    scope.textContent = scopes.style;
    prompt.value = '';
    dialog.showModal();
    document.body.classList.add('chart-editor-open');
    title.focus();
  }));

  dialog.addEventListener('change', (event) => {
    if (event.target.name === 'chart-edit-type') scope.textContent = scopes[event.target.value];
  });
  closeButton.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });
  dialog.addEventListener('close', () => {
    document.body.classList.remove('chart-editor-open');
    if (opener?.isConnected) opener.focus();
  });
})();
