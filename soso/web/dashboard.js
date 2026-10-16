'use strict';
const $ = id => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value; };
const el = (tag, content, cls) => { const n = document.createElement(tag); if(content !== undefined)n.textContent = content; if(cls)n.className=cls; return n; };
const fmt = n => n == null ? 'Unavailable' : Number(n).toLocaleString();
const duration = n => n == null ? 'Unknown' : n < 60 ? `${Number(n).toFixed(1)}s` : `${Math.floor(n/60)}m ${Math.floor(n%60)}s`;
const words = s => String(s || 'unknown').replaceAll('_',' ');
function empty(node, message){node.append(el('p',message,'empty'));}
function render(d){
 const focusedAsset=document.activeElement?.closest('#assets a')?.getAttribute('href');
 text('project', d.intent_title || 'Run progress');text('status', d.status === 'blocked' ? 'Stopped · needs attention' : words(d.status));text('subtitle', `Project: ${d.project} · ${d.mode === 'optimize' ? 'Self-improvement' : 'Project delivery'} · ${d.runner_active ? 'Runner is active' : 'No active launcher detected'}`);
 const retryNotice = d.next_retry_at ? `${d.reason || 'Waiting for the next attempt'}. ${d.runner_active ? 'Retrying automatically at' : 'Saved retry time:'} ${new Date(d.next_retry_at*1000).toLocaleString()}.` : d.reason;
 text('notice',retryNotice || (!d.runner_active ? 'Showing saved evidence. The runner may have finished or stopped.' : !d.telemetry_available && d.request_count>0 ? 'This run predates detailed usage telemetry. Available journal events are shown; missing metrics are not estimated.' : ''));
 const failure=[...d.timeline].reverse().find(e=>e.event==='call_finished' && e.status==='error');
 if(!d.runner_active && d.status==='blocked' && failure?.error_kind==='timeout')text('notice',`${failure.model || 'Model'} request timed out after ${duration(failure.seconds)}. The runner stopped before completing the goal.`);
 $('notice').dataset.tone=d.status==='blocked'?'error':d.status==='waiting'?'waiting':'info';
 text('next-action',d.runner_active ? (d.next_retry_at?'No action needed: the runner will retry automatically.':'') : d.status==='blocked'?'Next step: run ./so-so resume after addressing the recorded failure. Completed results are preserved.':d.status==='interrupted'?'Run ./so-so resume to continue the saved work.':'');
 const stages=d.roadmap || [];
 const here=stages.filter(s=>['active','paused','complete'].includes(s.state));
 $('roadmap').replaceChildren();
 for(const [i,stage] of stages.entries()){
  const item=el('li',undefined,`stage ${stage.state}`);
  item.append(el('span',String(i+1),'stage-number'),el('strong',stage.label),el('small',stage.description));
  const label={active:'Now',paused:'Paused here',observed:'Observed',upcoming:'Not reached',complete:'Reached'}[stage.state];
  item.append(el('span',label,'stage-status'));
  if(stage.state==='active')item.setAttribute('aria-current','step');
  $('roadmap').append(item);
 }
 text('roadmap-state',d.runner_active ? d.status==='waiting'?'Waiting to retry':'Live position':'Saved position');
 text('phase',d.calls.length ? d.calls.map(c=>c.purpose || words(c.role)).filter((v,i,a)=>a.indexOf(v)===i).join(' / ') : here.map(s=>s.label).join(' / ') || words(d.phase));
 text('freshness',d.last_event_at ? `Last event ${duration(Date.now()/1000-d.last_event_at)} ago` : 'No events recorded yet');
 text('progress',d.percent==null?'Not quantified':`${d.percent}%`);
 text('progress-note',d.milestones.length?'Reviewer-reported milestone completion':d.plan.length?'No fixed milestone total':'Waiting for a measurable plan');
 text('call-count',d.calls.length ? d.calls.map(c=>c.display_name || c.model_name || c.model).filter((v,i,a)=>a.indexOf(v)===i).join(' + ') : d.request_count ? 'Between requests' : 'Not yet observed');text('call-note',d.calls.length ? d.calls.map(c=>`${c.host_label ? c.host_label+' · ' : ''}${c.host} · route: ${c.model} · ${words(c.role)} · ${duration(c.elapsed_seconds)} ${c.live?'elapsed':'when stopped'}`).join(' / ') + ` · ${d.request_count ?? '—'} requests recorded` : `${d.request_count ?? '—'} requests recorded · no call in flight`);text('eta-note',d.eta_reason);text('report',d.report || 'No report yet');
 $('plan').replaceChildren();for(const task of d.plan){$('plan').append(el('p',task,'task'));}for(const m of d.milestones){const item=el('div',undefined,'task');item.append(el('span',words(m.status),'tag'),el('p',m.title));if(m.evidence)item.append(el('small',m.evidence));$('plan').append(item);}if(!d.plan.length&&!d.milestones.length)empty($('plan'),d.runner_active?'The planner has not returned a plan yet.':'No plan was returned before the runner stopped.');
 $('skills').replaceChildren(...d.skills.map(s=>el('span',s,'tag')));
 text('usage-note',d.usage_scope+' Cumulative model-call time sums recorded call durations, including active calls. Overlapping calls count separately, so this is not the task’s wall-clock duration. Tokens are reported by the provider after a response. Output tokens/s divides output tokens by total request time, including input processing and model loading; it is not pure decoding speed. Median response excludes failed calls. Unknown values are not estimated.');
 $('models').replaceChildren();
 const models = [...d.models].sort((a,b)=>Number(d.calls.some(c=>c.model===b.model && c.live))-Number(d.calls.some(c=>c.model===a.model && c.live)));
 for(const c of d.calls) if(!models.some(m=>m.model===c.model)) models.push({...c,calls:null});
 text('model-count',`${models.length} model${models.length===1?'':'s'} · ${d.calls.filter(c=>c.live).length} call${d.calls.filter(c=>c.live).length===1?'':'s'} in flight`);
 for(const m of models){
  const active=d.calls.filter(c=>c.model===m.model && c.live);
  const card=el('article',undefined,'model-card');
  const head=el('div',undefined,'model-heading');
  head.append(el('h3',m.display_name || m.model_name || m.model));
  head.append(el('span',active.length ? `${active.length} in flight` : m.last_status==='error' ? 'Last call failed' : 'Idle',active.length?'tag':'tag neutral'));
  card.append(head,el('p',`${m.host_label ? m.host_label+' · ' : ''}${m.host || 'Host not recorded'} · routing alias: ${m.model}`,'model-host'));
  card.append(el('small',m.model_name,'model-file'));
  const metrics=el('dl',undefined,'metrics');
  const values=[['Cumulative model-call time',duration(m.elapsed_seconds)],['Current requests elapsed',duration(m.active_seconds)],['Requests',fmt(m.calls)],['Input tokens',fmt(m.input_tokens)],['Output tokens',fmt(m.output_tokens)],['Median response',duration(m.typical_seconds)],['Output tokens/s · end-to-end',fmt(m.output_tokens_per_second)],['Transport errors',fmt(m.errors)],['Invalid replies',fmt(m.invalid_replies || 0)]];
  for(const [label,value] of values){const cell=el('div');cell.append(el('dt',label),el('dd',value));metrics.append(cell);}
  card.append(metrics);
  if(m.input_tokens == null || m.output_tokens == null)card.append(el('small','Token counts are reported after a response finishes, when supported by the provider.'));
  for(const c of active){
   const line=el('div',undefined,'request');
   line.append(el('strong',`${c.purpose || words(c.role)} · ${duration(c.elapsed_seconds)} elapsed`));
   line.append(el('p',`Input ${fmt(c.prompt_bytes)} bytes · context limit ${c.context_tokens == null ? fmt(c.context_bytes)+' bytes' : fmt(c.context_tokens)+' tokens'} · timeout ${duration(c.timeout)}`));
   if(c.timeout>0){const meter=el('progress');meter.max=c.timeout;meter.value=Math.min(c.elapsed_seconds,c.timeout);meter.setAttribute('aria-label','Elapsed request time relative to timeout, not task completion');line.append(meter,el('small','Time allowance used — not completion progress'));}
   card.append(line);
  }
  if(m.last_error)card.append(el('p',`Last failure: ${words(m.last_error)}.`, 'failure'));
  $('models').append(card);
 }
 if(!models.length)empty($('models'),'No model telemetry has been found yet. This does not establish that no model is running.');
 $('insights').replaceChildren();for(const metric of (d.insights || [])){const card=el('article');card.append(el('p',metric.label),el('strong',fmt(metric.value)),el('small',metric.unit || 'Recorded measurement'));$('insights').append(card);}$('insights').hidden=!d.insights?.length;$('rounds').replaceChildren();for(const r of d.rounds.slice().reverse()){const card=el('div',undefined,'task');card.append(el('strong',`Round ${r.round} · ${r.accepted?'Accepted':r.outcome==='execution_failed'?'Execution failed — not a plateau':'No accepted change'}`));for(const e of r.experiments){card.append(el('p',e.task || 'Experiment'));card.append(el('small',e.error || `${e.evaluation?.valid===true?'Checks passed':e.evaluation?.valid===false?'Checks failed':'Checks not recorded'} · score ${fmt(e.evaluation?.score)}`));} $('rounds').append(card);}if(!d.rounds.length)empty($('rounds'),d.baseline?`Baseline: ${d.baseline.valid?'checks passed':'checks failed'} · ${d.baseline.checks?.length || 0} check commands recorded. No completed round yet.`:'No completed round yet. Baseline evaluation and planning happen first.');
 text('event-count',`· ${d.timeline.length} recent events`);$('timeline').replaceChildren();for(const event of d.timeline.slice().reverse()){const item=el('li');item.append(el('time',event.time?new Date(event.time*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'—'));item.append(el('span',[words(event.event),event.model,event.role,event.phase,event.tool,event.status,event.error,event.error_kind,event.problem,event.attempt ? `attempt ${event.attempt}` : '',event.retrying ? 'repairing response' : '',event.input_tokens==null?'':`${fmt(event.input_tokens)} input tokens`,event.output_tokens==null?'':`${fmt(event.output_tokens)} output tokens`,event.prompt_bytes==null?'':`${fmt(event.prompt_bytes)} input bytes`,event.seconds==null?'':duration(event.seconds)].filter(Boolean).join(' · ')));$('timeline').append(item);}
 text('asset-count',`· ${d.images.length} images`);$('assets').replaceChildren();for(const asset of d.images){const f=el('figure'),a=el('a'),img=el('img');a.href='asset/'+encodeURIComponent(asset.path);a.target='_blank';a.rel='noopener';img.src=a.href;img.alt=asset.name;img.loading='lazy';a.append(img);f.append(a,el('figcaption',asset.name));$('assets').append(f);}if(!d.images.length)empty($('assets'),'No image assets found in the result worktree.');
 if(focusedAsset)Array.from($('assets').querySelectorAll('a')).find(a=>a.getAttribute('href')===focusedAsset)?.focus({preventScroll:true});
}
let paused=false; $('pause').addEventListener('click',()=>{paused=!paused;text('pause',paused?'Resume updates':'Pause updates');text('connection',paused?'Updates paused':'Connecting…');});
async function tick(){if(!paused){try{const response=await fetch('api',{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error('unavailable');render(await response.json());text('connection','Connected');}catch(e){text('connection','Disconnected · retrying');text('notice','Monitor connection lost. Last displayed evidence is retained. The launcher may have exited; use attach mode to inspect saved results.');}}setTimeout(tick,2000);}tick();
