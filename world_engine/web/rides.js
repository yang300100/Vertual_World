/* 哥哥，共乘的接受、登车和驾驶分别由本人决定。 */
const rideStatuses = {pending:"等待逐人回应",accepted:"已同意，等待分别登车",boarding:"等待分别登车",moving:"行驶中",arrived:"已安全靠站",blocked:"已暂停，保留实际乘员位置",suspended:"已暂停，仍留在车内",stopped:"已安全靠站",completed:"已完成",ended:"已结束",expired:"期限已到",declined:"已有成员婉拒"};
const rideLive = new Set(["pending","accepted","boarding","moving","arrived","blocked","suspended","stopped"]);
Object.assign(eventLabels, {
  "social.ride_proposed":"提出有限共乘", "social.ride_accepted":"本人接受共乘", "social.ride_declined":"本人婉拒共乘",
  "social.ride_boarded":"本人实际登车", "social.ride_departed":"载具实际出发", "social.ride_stop_requested":"请求安全靠站",
  "social.ride_arrived":"载具实际靠站", "social.ride_disembarked":"本人实际下车", "social.ride_ended":"共乘约定结束",
  "social.ride_suspended":"载具行程暂停", "social.ride_completed":"共乘完成", "social.ride_expired":"共乘期限已到",
  "social.ride_recovery_proposed":"提出续程方案", "social.ride_recovery_accepted":"本人同意续程方案",
  "social.ride_recovery_declined":"本人婉拒续程方案", "social.ride_recovery_withdrawn":"撤回续程方案",
  "social.ride_recovery_resumed":"原驾驶者恢复续程",
  "social.ride_handoff_proposed":"提出有限驾驶交接", "social.ride_handoff_accepted":"本人同意驾驶交接",
  "social.ride_handoff_declined":"本人婉拒驾驶交接", "social.ride_handoff_authorized":"持有人授予本次驾驶许可",
  "social.ride_handoff_withdrawn":"本人撤回交接授权", "social.ride_handoff_taken":"本人实际接管驾驶",
  "social.ride_handoff_revoked":"持有人撤回本次驾驶许可", "social.ride_handoff_invalidated":"驾驶交接条件已变化",
});
let rideContextKey = "", rideContextEpoch = 0, rideRead = null, rideReadAt = 0;
const rideActions = new Set(), rideDrafts = new Map(), rideAsked = new Set();
const rideRecoveryDrafts = new Map();
const rideHandoffDrafts = new Map();
const rideHandoffStatuses = {pending:"等待逐人回应和驾驶许可",accepted:"成员已同意，等待驾驶许可及接管决定",declined:"已保留成员婉拒",withdrawn:"已撤回交接方案",expired:"交接方案期限已到",invalidated:"原停留或权限已变化",executed:"已按方案交接驾驶",revoked:"驾驶许可已撤回，保持实际乘员位置"};
const rideRecoveryStatuses = {pending:"等待在车成员分别回应",accepted:"成员已同意，等待原驾驶者决定",declined:"已保留本人婉拒",withdrawn:"提出者已撤回",expired:"续程方案期限已到",invalidated:"原停留状态已变化",executed:"已按方案恢复原路线"};
function rideContext() {
  const worldId = state.worldId, playerId = currentPlayer()?.id, key = worldId && playerId ? worldId + ":" + playerId : "";
  if (key !== rideContextKey) { if (rideRead) rideRead.invalidated = true; rideContextKey = key; rideContextEpoch += 1; rideReadAt = 0; }
  return {worldId,playerId,key,epoch:rideContextEpoch};
}
function rideCurrent(context) { const now = rideContext(); return Boolean(context?.key) && context.key === now.key && context.epoch === now.epoch; }
function rideRows(value) { return Array.isArray(value) ? value : value?.rides || []; }
function rideText(parent,text,tag="p") { const node=document.createElement(tag); node.textContent=text; parent.append(node); return node; }
function rideField(parent,label,tag="input") { const box=document.createElement("label"),input=document.createElement(tag); box.textContent=label;input.setAttribute("aria-label",label);box.append(input);parent.append(box);return input; }
function rideOption(select,value,label) { const node=document.createElement("option");node.value=value;node.textContent=label;select.append(node); }
function rideMember(ride,id) { return (ride.members || []).find(member=>member.character_id===id); }
function rideDriverId(ride) { return ride.effective_controller_id || ride.controller_id; }
function rideContract(ride) {
  return JSON.stringify([ride.id,ride.proposer_id,ride.controller_id,ride.vehicle_id,ride.destination_location_id,ride.deadline_at,ride.statement,
    (ride.members||[]).map(member=>member.character_id)]);
}
function rideVersion(ride) {
  return JSON.stringify([rideContract(ride),ride.status,ride.movement_id,ride.source_event_id,ride.stop_requested,ride.safe_parked,ride.can_disembark,
    (ride.members||[]).map(member=>[member.character_id,member.decision,member.decision_at,member.boarded_at,member.disembarked_at]),rideRecoveryVersion(ride),rideHandoffVersion(ride)]);
}
function rideRecoveryContract(ride) {
  const recovery=ride.recovery;
  return JSON.stringify([rideContract(ride),rideDriverId(ride),ride.status,ride.movement_id,ride.stop_requested,
    (ride.members||[]).map(member=>[member.character_id,member.boarded_at,member.disembarked_at,member.board_event_id]),
    recovery?[recovery.id,recovery.epoch,recovery.proposer_id,recovery.controller_id,recovery.movement_id,recovery.deadline_at,recovery.terms,
      (recovery.members||[]).map(member=>member.character_id)]:null]);
}
function rideRecoveryVersion(ride) {
  const recovery=ride.recovery;
  return JSON.stringify([rideRecoveryContract(ride),ride.can_propose_recovery,ride.effective_deadline_at,
    recovery?[recovery.status,recovery.source_event_id,recovery.can_accept,recovery.can_decline,recovery.can_withdraw,recovery.can_resume,
      (recovery.members||[]).map(member=>[member.character_id,member.decision,member.decision_at,member.decision_event_id])]:null]);
}
function rideRecoveryAllowed(ride,playerId,operation) {
  const member=rideMember(ride,playerId),recovery=ride.recovery;
  if(ride.status!=="blocked"||!member?.boarded_at||member.disembarked_at)return false;
  if(operation==="propose_recovery")return ride.can_propose_recovery===true;
  if(!recovery||!(recovery.members||[]).some(person=>person.character_id===playerId))return false;
  if(operation==="accept_recovery")return recovery.status==="pending"&&recovery.can_accept===true;
  if(operation==="decline_recovery")return recovery.status==="pending"&&recovery.can_decline===true;
  if(operation==="withdraw_recovery")return ["pending","accepted"].includes(recovery.status)&&recovery.can_withdraw===true;
  if(operation==="resume")return recovery.status==="accepted"&&rideDriverId(ride)===playerId&&(recovery.controller_id||rideDriverId(ride))===playerId&&recovery.can_resume===true&&
    (recovery.members||[]).every(person=>person.decision==="accepted")&&
    (ride.members||[]).every(person=>person.boarded_at&&!person.disembarked_at);
  return false;
}
function rideAllowed(ride,playerId,operation) {
  const member=rideMember(ride,playerId), aboard=member?.boarded_at && !member.disembarked_at;
  if (!member || !rideLive.has(ride.status)) return false;
  if (operation==="accept" || operation==="decline") return ride.status==="pending" && member.decision==="pending";
  if (operation==="board") return ["accepted","boarding"].includes(ride.status) && member.decision==="accepted" && !member.boarded_at;
  if (operation==="depart") return ride.controller_id===playerId && ["accepted","boarding"].includes(ride.status) &&
    (ride.members||[]).every(person=>person.decision==="accepted" && person.boarded_at && !person.disembarked_at);
  if (operation==="request_stop") return Boolean(aboard) && ["moving","blocked"].includes(ride.status);
  if (operation==="disembark") return Boolean(aboard) && (["accepted","boarding","arrived","stopped"].includes(ride.status) || ride.status==="blocked" && (ride.can_disembark===true || ride.safe_parked===true));
  if (operation==="end") return ride.proposer_id===playerId && ["pending","accepted","boarding","arrived","stopped","blocked"].includes(ride.status) &&
    !(ride.members||[]).some(person=>person.boarded_at&&!person.disembarked_at);
  return false;
}
function rideRecipient(ride,playerId) { return [rideDriverId(ride),ride.proposer_id,...(ride.members||[]).map(member=>member.character_id)].find(id=>id && id!==playerId && rideMember(ride,id)) || null; }
async function submitRide(context,recipient,choice) {
  if (!rideCurrent(context)) return null;
  if (rideRead?.context.key===context.key) rideRead.invalidated=true;
  rideReadAt=0;
  return executeNaturalSocialChoice(choice.statement,{target_character_id:recipient,choice_evidence:choice.statement,ride_choice:choice});
}
function appendRideRecovery(parent,ride,context,isCurrent=()=>true) {
  if(!ride.recovery&&ride.can_propose_recovery!==true)return;
  const section=document.createElement("section");section.className="ride-recovery";parent.append(section);
  const current=()=>section.isConnected&&rideCurrent(context)&&isCurrent(),recovery=ride.recovery;
  rideText(section,"商量继续原路线","h4");
  if(recovery) {
    rideText(section,rideRecoveryStatuses[recovery.status]||"续程方案记录");
    rideText(section,"新期限："+formatWorldTime(recovery.deadline_at)+"；驾驶者、在车名单和原目的地保持不变。");
    const terms=recovery.terms||{},distance=Number(terms.remaining_distance_km),seconds=Number(terms.remaining_seconds);
    if(Number.isFinite(distance)&&distance>=0&&Number.isFinite(seconds)&&seconds>=0)rideText(section,"原路线剩余约"+distance.toFixed(2)+"公里，预计约"+Math.ceil(seconds/60)+"个世界分钟。");
    if(terms.stop_requested||ride.stop_requested)rideText(section,"原安全靠站请求仍然保留，继续行驶只为按原路线到原安全目的地。");
    for(const member of recovery.members||[])rideText(section,(member.character_id===context.playerId?"你":member.name||"在车成员")+"："+(member.decision==="accepted"?"本人已同意续程":member.decision==="declined"?"本人已婉拒续程":"尚未回应续程方案"));
    const notice=rideText(section,"");notice.setAttribute("aria-live","polite");
    const choose=(label,operation,statement)=>{
      if(!rideRecoveryAllowed(ride,context.playerId,operation))return;
      const button=document.createElement("button");button.type="button";button.className="button secondary";button.dataset.rideAction=operation;button.textContent=label;section.append(button);
      button.addEventListener("click",async()=>{
        if(!current()||state.busy||state.responsePending||rideActions.has(context.key))return;
        rideActions.add(context.key);button.disabled=true;
        try {
          const latest=rideRows(await api("/api/worlds/"+context.worldId+"/rides")).find(value=>value.id===ride.id);
          if(!current())return;
          if(!latest||!rideRecoveryAllowed(latest,context.playerId,operation)||rideRecoveryVersion(latest)!==rideRecoveryVersion(ride)) {
            notice.textContent="续程方案、停留状态或成员回应已变化，请刷新后重新决定。";return;
          }
          const recipient=rideRecipient(latest,context.playerId);if(!recipient){notice.textContent="无法核对原在车成员，请刷新记录。";return;}
          const result=await submitRide(context,recipient,{operation,ride_id:latest.id,recovery_id:latest.recovery.id,statement});
          if(!current())return;
          if(result?.event_id)await renderMyRides(true);else notice.textContent="这次续程选择尚未确认保存，原方案与回应已保留。";
        }catch{if(current())notice.textContent="暂时无法保存续程选择，原方案与回应已保留。";}
        finally{rideActions.delete(context.key);if(current())button.disabled=false;}
      });
    };
    choose("仅我同意这份续程方案","accept_recovery","我本人同意这份原驾驶者、原路线、原在车名单和新期限都明确的续程方案；不替任何人答应，也不代表现在恢复驾驶。");
    choose("仅我婉拒这份续程方案","decline_recovery","我本人暂不同意这份续程方案，请保留我的回应及原来实际在车位置，不自动继续行驶。");
    if(recovery.proposer_id===context.playerId)choose("撤回我提出的续程方案","withdraw_recovery","我撤回本人提出的这份续程方案，保留已经发生的回应、实际位置和安全靠站请求。");
    else choose("撤回我本人的续程授权","withdraw_recovery","我撤回本人已经表达的这份续程授权，请保留原回应历史、实际位置和安全靠站请求，不继续驾驶。");
    choose("由我恢复原路线","resume","我作为原驾驶者本人选择按大家重新同意的这份方案继续原路线；请核对真实驾驶权、身体、期限和全部在车成员的授权，保留原安全靠站请求。");
    const stage=recovery.status==="pending"?"accept_recovery":recovery.status==="accepted"?"resume":null;
    const aboard=(ride.members||[]).filter(member=>member.boarded_at&&!member.disembarked_at);
    const needsReply=stage&&ride.status==="blocked"&&aboard.length===(recovery.members||[]).length&&(recovery.members||[]).some(member=>member.character_id!==context.playerId&&
      (stage==="accept_recovery"?member.decision==="pending":member.character_id===(recovery.controller_id||rideDriverId(ride))&&member.decision==="accepted"));
    if(needsReply) {
      const button=document.createElement("button");button.type="button";button.className="button secondary";button.dataset.rideAction="ask_"+stage;
      button.textContent=stage==="accept_recovery"?"询问在车成员的续程意愿":"询问原驾驶者是否恢复";
      const questionKey=context.key+":"+context.epoch+":"+ride.id+":"+recovery.id+":"+recovery.epoch+":"+stage;
      const canAsk=()=>typeof rideRecoveryHasPendingReplies==="function"?rideRecoveryHasPendingReplies(ride,stage):!rideAsked.has(questionKey);
      button.disabled=!canAsk();section.append(button);
      button.addEventListener("click",async()=>{
        if(!current()||state.busy||state.responsePending||rideActions.has(context.key)||!canAsk())return;
        rideActions.add(context.key);rideAsked.add(questionKey);button.disabled=true;
        try {
          await requestRideRecoveryReplies(naturalSocialContext(),{operation:"ask",ride_id:ride.id,recovery_id:recovery.id,stage},
            "想听听你本人对这份原路线续程方案的决定。",rideRecoveryVersion(ride));
          if(current())await renderMyRides(true);
        }catch{if(current())notice.textContent="成员回应暂未完成；原方案、实际位置和已表达的意愿都保留，不会自动再问。";}
        finally{rideActions.delete(context.key);if(current())button.disabled=!canAsk();}
      });
    }
  }
  if(!rideRecoveryAllowed(ride,context.playerId,"propose_recovery"))return;
  const form=document.createElement("form");form.className="long-term-form ride-recovery-form";section.append(form);
  const key=context.key+":"+ride.id,draft=rideRecoveryDrafts.get(key)||{},formCurrent=()=>current()&&form.isConnected;
  const deadline=rideField(form,"续程新期限（世界时间 UTC）");deadline.type="datetime-local";deadline.required=true;
  const at=Date.parse(state.living?.world_time||"");deadline.value=draft.deadline||(Number.isFinite(at)?new Date(at+2*3600000).toISOString().slice(0,16):"");
  const speech=rideField(form,"想怎样商量恢复原路线","textarea");speech.required=true;speech.maxLength=100;speech.value=draft.statement||"";
  rideText(form,"只商量原路线的剩余路程和新期限。每位在车成员分别回应后，仍由原驾驶者另行决定是否恢复；这不会延长租约或补充物资。");
  if(ride.stop_requested)rideText(form,"原安全靠站请求仍保留，目的地不会改动。");
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出续程方案，分别听取意愿";form.append(submit);
  const notice=rideText(form,"");notice.setAttribute("aria-live","polite");
  const save=()=>{if(rideCurrent(context))rideRecoveryDrafts.set(key,{deadline:deadline.value,statement:speech.value});};
  form.rideRecoverySaveDraft=save;deadline.addEventListener("input",save);speech.addEventListener("input",save);
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!formCurrent()||state.busy||state.responsePending||rideActions.has(context.key)||!form.reportValidity())return;
    const end=new Date(deadline.value+"Z");if(!Number.isFinite(end.getTime())||Number.isFinite(at)&&(end.getTime()<=at||end.getTime()>at+24*3600000)){notice.textContent="请明确现在之后、24个世界小时以内的新期限。";return;}
    const words=speech.value.trim();if(!words){notice.textContent="请先写下本人想怎样商量续程。";return;}
    const choice={operation:"propose_recovery",ride_id:ride.id,deadline_world_time:end.toISOString(),statement:words+" 我只提出原驾驶者、原路线及原在车名单的续程方案和表内新期限；每个人分别决定，原安全靠站请求保留。"};
    save();const submitted={deadline:deadline.value,statement:speech.value};rideActions.add(context.key);const inputs=form.querySelectorAll("input,textarea,button");inputs.forEach(input=>{input.disabled=true;});
    try {
      const latest=rideRows(await api("/api/worlds/"+context.worldId+"/rides")).find(value=>value.id===ride.id);
      if(!formCurrent())return;
      if(!latest||!rideRecoveryAllowed(latest,context.playerId,"propose_recovery")||rideRecoveryVersion(latest)!==rideRecoveryVersion(ride)) {
        notice.textContent="原停留状态、在车成员或续程方案已变化，请刷新核对；原话与期限草稿已保留。";return;
      }
      const recipient=rideRecipient(latest,context.playerId);if(!recipient){notice.textContent="无法核对原在车成员，请刷新记录。";return;}
      const result=await submitRide(context,recipient,choice);if(!formCurrent())return;
      if(result?.event_id){const saved=rideRecoveryDrafts.get(key);if(saved?.deadline===submitted.deadline&&saved.statement===submitted.statement)rideRecoveryDrafts.delete(key);form.rideRecoverySaveDraft=null;await renderMyRides(true);}
      else notice.textContent="续程提议尚未确认保存，原话与期限草稿已保留。";
    }catch{if(formCurrent())notice.textContent="暂时无法保存续程提议，原话与期限草稿已保留。";}
    finally{rideActions.delete(context.key);if(formCurrent())inputs.forEach(input=>{input.disabled=false;});}
  });
}
function rideHandoffContract(ride) {
  const handoff=ride.handoff,authority=handoff?.authority||{};
  return JSON.stringify([rideContract(ride),rideDriverId(ride),ride.status,ride.movement_id,ride.stop_requested,
    (ride.members||[]).map(member=>[member.character_id,member.boarded_at,member.disembarked_at,member.board_event_id]),
    handoff?[handoff.id,handoff.epoch,handoff.terms,(handoff.members||[]).map(member=>member.character_id),
      authority.holder_id,authority.source_kind,authority.source_id]:null]);
}
function rideHandoffVersion(ride) {
  const handoff=ride.handoff;
  return JSON.stringify([rideHandoffContract(ride),ride.can_propose_handoff,
    handoff?[handoff.status,handoff.source_event_id,handoff.authority,handoff.can_accept,handoff.can_decline,handoff.can_withdraw,
      handoff.can_authorize,handoff.can_take_control,handoff.can_revoke,
      (handoff.members||[]).map(member=>[member.character_id,member.decision,member.decision_at,member.decision_event_id])]:null]);
}
function rideHandoffRosterCurrent(ride) {
  const actual=(ride.members||[]).map(member=>member.character_id),handoff=ride.handoff;
  const frozen=handoff?.terms?.member_ids,decisions=(handoff?.members||[]).map(member=>member.character_id);
  return Array.isArray(frozen)&&actual.length>0&&new Set(actual).size===actual.length&&
    frozen.length===actual.length&&new Set(frozen).size===frozen.length&&frozen.every(id=>actual.includes(id))&&
    decisions.length===actual.length&&new Set(decisions).size===decisions.length&&decisions.every(id=>actual.includes(id));
}
function rideHandoffAllowed(ride,playerId,operation) {
  const own=rideMember(ride,playerId),handoff=ride.handoff;
  if(!own?.boarded_at||own.disembarked_at)return false;
  if(operation==="propose_handoff")return ride.status==="blocked"&&ride.can_propose_handoff===true;
  if(!handoff||!(handoff.members||[]).some(member=>member.character_id===playerId))return false;
  const authority=handoff.authority||{},driver=handoff.terms?.controller_id||handoff.controller_id;
  if(operation==="revoke_handoff")return handoff.status==="executed"&&authority.holder_id===playerId&&handoff.can_revoke===true;
  if(!rideHandoffRosterCurrent(ride))return false;
  if(ride.status!=="blocked"||!["pending","accepted"].includes(handoff.status))return false;
  if(operation==="accept_handoff")return handoff.can_accept===true;
  if(operation==="decline_handoff")return handoff.can_decline===true;
  if(operation==="withdraw_handoff")return handoff.can_withdraw===true;
  if(operation==="authorize_handoff")return authority.holder_id===playerId&&handoff.can_authorize===true;
  if(operation==="take_control")return driver===playerId&&handoff.can_take_control===true&&
    (handoff.members||[]).every(member=>member.decision==="accepted")&&Boolean(authority.grant_event_id)&&!authority.revoke_event_id&&
    (ride.members||[]).every(member=>member.boarded_at&&!member.disembarked_at);
  return false;
}
function appendRideHandoff(parent,ride,context,isCurrent=()=>true) {
  if(!ride.handoff&&ride.can_propose_handoff!==true)return;
  const section=document.createElement("section");section.className="ride-handoff";parent.append(section);
  const current=()=>section.isConnected&&rideCurrent(context)&&isCurrent(),handoff=ride.handoff;
  rideText(section,"商量交接驾驶","h4");
  rideText(section,"每位在车成员分别决定，合法持有人另行给予本次驾驶许可，再由接驾驶者本人决定接管；不自动延租、付费或改变目的地。");
  if(handoff) {
    const terms=handoff.terms||{},authority=handoff.authority||{},name=id=>id===context.playerId?"你":rideMember(ride,id)?.name||"原在车成员";
    rideText(section,rideHandoffStatuses[handoff.status]||"交接方案记录");
    rideText(section,"拟由"+name(terms.controller_id||handoff.controller_id)+"接驾驶，期限："+formatWorldTime(terms.deadline_at||handoff.deadline_at));
    rideText(section,"本次许可须由"+name(authority.holder_id)+"作为合法持有人另行决定。");
    rideText(section,"本次驾驶许可："+(authority.revoke_event_id||handoff.status==="revoked"?"持有人已撤回":authority.grant_event_id?"持有人已明确授予":"等待合法持有人另行决定"));
    const distance=Number(terms.remaining_distance_km),seconds=Number(terms.remaining_seconds);
    if(Number.isFinite(distance)&&distance>=0&&Number.isFinite(seconds)&&seconds>=0)rideText(section,"原路线剩余约"+distance.toFixed(2)+"公里，预计约"+Math.ceil(seconds/60)+"个世界分钟。");
    if(terms.stop_requested||ride.stop_requested)rideText(section,"原安全靠站请求仍保留，只继续原路线余程到原安全目的地。");
    for(const member of handoff.members||[])rideText(section,name(member.character_id)+"："+(member.decision==="accepted"?"本人已同意交接":member.decision==="declined"?"本人已婉拒交接":"尚未回应交接方案"));
    const notice=rideText(section,"");notice.setAttribute("aria-live","polite");
    const choose=(label,operation,statement)=>{
      if(!rideHandoffAllowed(ride,context.playerId,operation))return;
      const button=document.createElement("button");button.type="button";button.className="button secondary";button.dataset.rideAction=operation;button.textContent=label;section.append(button);
      button.addEventListener("click",async()=>{
        if(!current()||state.busy||state.responsePending||rideActions.has(context.key))return;
        rideActions.add(context.key);button.disabled=true;
        try {
          const latest=rideRows(await api("/api/worlds/"+context.worldId+"/rides")).find(value=>value.id===ride.id);
          if(!current())return;
          if(!latest||!rideHandoffAllowed(latest,context.playerId,operation)||rideHandoffVersion(latest)!==rideHandoffVersion(ride)) {
            notice.textContent="交接方案、成员或真实驾驶许可已变化，请刷新后重新决定。";return;
          }
          const recipient=rideRecipient(latest,context.playerId);if(!recipient){notice.textContent="无法核对原在车成员，请刷新记录。";return;}
          const result=await submitRide(context,recipient,{operation,ride_id:latest.id,handoff_id:latest.handoff.id,statement});
          if(!current())return;
          if(result?.event_id)await renderMyRides(true);else notice.textContent="这次选择尚未确认保存，原方案及各人回应已保留。";
        }catch{if(current())notice.textContent="暂时无法保存交接选择，原方案及各人回应已保留。";}
        finally{rideActions.delete(context.key);if(current())button.disabled=false;}
      });
    };
    choose("仅我同意这份交接方案","accept_handoff","我本人同意这份明确接驾驶者、原路线、完整在车名单和新期限的交接方案；不代他人接受，也不授予驾驶许可或自动开车。");
    choose("仅我婉拒这份交接方案","decline_handoff","我本人暂不同意这份交接方案，请保留我的回应和实际在车位置，不自动继续行驶。");
    choose(handoff.proposer_id===context.playerId?"撤回我提出的交接方案":"撤回我本人的交接同意","withdraw_handoff","我撤回本人提出的交接方案或本人已经表达的交接同意，保留原回应、原路线、实际位置和安全靠站请求。");
    choose("仅授予本次有限驾驶许可","authorize_handoff","我作为当前合法持有人，仅对这份交接方案、接驾驶者、原路线和新期限给予本次有限驾驶许可；不转移财产、租约，也不代表全员同意或现在开车。");
    choose("由我接管并续行原路线","take_control","我作为这份方案的接驾驶者本人选择接管并继续原路线；请核对合法持有人的本次许可、全员独立同意、身体和期限，保留原安全靠站请求。");
    choose("撤回我授予的驾驶许可","revoke_handoff","我撤回本人授予这份方案的有限驾驶许可，请原位挂起并保留全部乘员和实际位置，不改回旧驾驶者，也不替任何人下车。");
    for(const [stage,label] of [["accept_handoff","询问在车成员的交接意愿"],["authorize_handoff","询问持有人是否给予驾驶许可"],["take_control","询问接驾驶者是否接管"]]) {
      if(typeof rideHandoffHasPendingReplies!=="function"||!rideHandoffHasPendingReplies(ride,stage))continue;
      const button=document.createElement("button");button.type="button";button.className="button secondary";button.dataset.rideAction="ask_"+stage;button.textContent=label;section.append(button);
      button.addEventListener("click",async()=>{
        if(!current()||state.busy||state.responsePending||rideActions.has(context.key)||!rideHandoffHasPendingReplies(ride,stage))return;
        rideActions.add(context.key);button.disabled=true;
        try {
          await requestRideHandoffReplies(naturalSocialContext(),{operation:"ask",ride_id:ride.id,handoff_id:handoff.id,stage},"想听听你本人对眼前这份交接方案的独立决定。",rideHandoffVersion(ride));
          if(current())await renderMyRides(true);
        }catch{if(current())notice.textContent="成员回应暂未完成，原方案及已表达的意愿保留，不会自动重问。";}
        finally{rideActions.delete(context.key);if(current())button.disabled=!rideHandoffHasPendingReplies(ride,stage);}
      });
    }
  }
  if(!rideHandoffAllowed(ride,context.playerId,"propose_handoff"))return;
  const form=document.createElement("form");form.className="long-term-form ride-handoff-form";section.append(form);
  const key=context.key+":"+ride.id,draft=rideHandoffDrafts.get(key)||{},formCurrent=()=>current()&&form.isConnected;
  const candidates=(ride.members||[]).filter(member=>member.boarded_at&&!member.disembarked_at);
  const driver=rideField(form,"拟由谁接驾驶","select");driver.required=true;
  for(const member of candidates)rideOption(driver,member.character_id,member.character_id===context.playerId?"你":member.name||"原在车成员");
  driver.value=draft.controller_id||candidates.find(member=>member.character_id!==rideDriverId(ride))?.character_id||candidates[0]?.character_id||"";
  const deadline=rideField(form,"交接新期限（世界时间 UTC）");deadline.type="datetime-local";deadline.required=true;
  const at=Date.parse(state.living?.world_time||"");deadline.value=draft.deadline||(Number.isFinite(at)?new Date(at+2*3600000).toISOString().slice(0,16):"");
  const speech=rideField(form,"想怎样商量交接驾驶","textarea");speech.required=true;speech.maxLength=100;speech.value=draft.statement||"";
  rideText(form,"只可选择原在车成员。这一步只是提出方案，不能替任何人接受或授予驾驶许可，也不会自动接管、延租或补给。");
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出交接方案，分别听取意愿";form.append(submit);
  const notice=rideText(form,"");notice.setAttribute("aria-live","polite");
  const save=()=>{if(rideCurrent(context))rideHandoffDrafts.set(key,{controller_id:driver.value,deadline:deadline.value,statement:speech.value});};
  form.rideHandoffSaveDraft=save;for(const input of [driver,deadline,speech]){input.addEventListener("input",save);input.addEventListener("change",save);}
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!formCurrent()||state.busy||state.responsePending||rideActions.has(context.key)||!form.reportValidity())return;
    const end=new Date(deadline.value+"Z"),words=speech.value.trim();
    if(!candidates.some(member=>member.character_id===driver.value)||!Number.isFinite(end.getTime())||Number.isFinite(at)&&(end.getTime()<=at||end.getTime()>at+24*3600000)) {notice.textContent="请选原在车成员，并明确现在之后24个世界小时以内的新期限。";return;}
    if(!words){notice.textContent="请先写下本人想怎样商量交接。";return;}
    const choice={operation:"propose_handoff",ride_id:ride.id,controller_id:driver.value,deadline_world_time:end.toISOString(),statement:words+" 我只提出由表内原在车成员接驾驶、沿原路线和表内新期限的方案；各人分别决定，合法持有人另行授权，原安全靠站请求保留。"};
    save();const submitted={controller_id:driver.value,deadline:deadline.value,statement:speech.value};rideActions.add(context.key);const inputs=form.querySelectorAll("input,select,textarea,button");inputs.forEach(input=>{input.disabled=true;});
    try {
      const latest=rideRows(await api("/api/worlds/"+context.worldId+"/rides")).find(value=>value.id===ride.id);
      if(!formCurrent())return;
      if(!latest||!rideHandoffAllowed(latest,context.playerId,"propose_handoff")||rideHandoffVersion(latest)!==rideHandoffVersion(ride)||!(latest.members||[]).some(member=>member.character_id===choice.controller_id&&member.boarded_at&&!member.disembarked_at)) {
        notice.textContent="原停留、在车名单或驾驶权已变化，请刷新核对；完整草稿已保留。";return;
      }
      const recipient=rideRecipient(latest,context.playerId);if(!recipient){notice.textContent="无法核对原在车成员，请刷新记录。";return;}
      const result=await submitRide(context,recipient,choice);if(!formCurrent())return;
      if(result?.event_id){const saved=rideHandoffDrafts.get(key);if(saved?.controller_id===submitted.controller_id&&saved.deadline===submitted.deadline&&saved.statement===submitted.statement)rideHandoffDrafts.delete(key);form.rideHandoffSaveDraft=null;await renderMyRides(true);}
      else notice.textContent="交接提议尚未确认保存，原话、接驾驶者和期限草稿已保留。";
    }catch{if(formCurrent())notice.textContent="暂时无法保存交接提议，完整草稿已保留。";}
    finally{rideActions.delete(context.key);if(formCurrent())inputs.forEach(input=>{input.disabled=false;});}
  });
}
function rideRow(ride,context,isCurrent=()=>true) {
  const row=document.createElement("article");row.className="life-item ride-row";row.dataset.rideId=ride.id;
  const current=()=>row.isConnected && rideCurrent(context) && isCurrent();
  rideText(row,(ride.vehicle_name || ride.vehicle?.name || "原约定载具")+" · "+(rideStatuses[ride.status]||"共乘记录"),"strong");
  rideText(row,"目的地："+(ride.destination_name||"原约定地点")+" · 当前期限："+formatWorldTime(ride.effective_deadline_at||ride.deadline_at));
  const driverId=rideDriverId(ride);
  rideText(row,"当前驾驶者："+(driverId===context.playerId?"你":ride.effective_controller_name||rideMember(ride,driverId)?.name||ride.controller_name||"原约定成员")+"。同意共乘后，每个人仍须分别决定登车。");
  const members=document.createElement("ul");row.append(members);
  for(const member of ride.members||[]) {
    const status=member.disembarked_at?"本人已下车":member.boarded_at?"本人已登车":member.decision==="accepted"?"本人已同意，尚未登车":member.decision==="declined"?"本人已婉拒":"尚未答复";
    rideText(members,(member.character_id===context.playerId?"你":member.name||"原约定成员")+"："+status,"li");
  }
  rideText(row,ride.statement||"");if(ride.reason)rideText(row,ride.reason);
  if(ride.status==="moving")rideText(row,"载具正在实际行驶。需要离开时可以请求安全靠站，靠站前仍留在车内。");
  if(["suspended","blocked"].includes(ride.status))rideText(row,"载具已暂停，乘员仍保留在车内。请等待驾驶权及安全靠站条件恢复。");
  appendRideRecovery(row,ride,context,current);
  appendRideHandoff(row,ride,context,current);
  const notice=rideText(row,"");notice.setAttribute("aria-live","polite");let stale=false;
  const controls=()=>row.querySelectorAll("button[data-ride-action]");
  async function act(operation,statement) {
    if(!current()||stale||state.busy||state.responsePending||rideActions.has(context.key))return;
    rideActions.add(context.key);controls().forEach(button=>{button.disabled=true;});
    try {
      const latest=rideRows(await api("/api/worlds/"+context.worldId+"/rides")).find(value=>value.id===ride.id);
      if(!current())return;
      const flexible=["request_stop","disembark","end"].includes(operation);
      if(!latest||!rideAllowed(latest,context.playerId,operation)||(flexible?rideContract(latest)!==rideContract(ride):rideVersion(latest)!==rideVersion(ride))) {
        stale=true;notice.textContent="这份共乘或成员状态已变化，请刷新后再决定。";return;
      }
      const recipient=rideRecipient(latest,context.playerId);if(!recipient){notice.textContent="无法核对原成员，请刷新记录。";return;}
      const result=await submitRide(context,recipient,{operation,ride_id:latest.id,statement});
      if(!current())return;
      if(result?.event_id)await renderMyRides(true);else notice.textContent="这次选择尚未确认保存，原记录已保留，请稍后核对。";
    } catch {if(current())notice.textContent="暂时无法核对或保存选择，原记录已保留，请稍后重试。";}
    finally {rideActions.delete(context.key);if(current())controls().forEach(button=>{button.disabled=stale;});}
  }
  const choose=(label,operation,statement)=>{
    if(!rideAllowed(ride,context.playerId,operation))return;
    const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent=label;button.dataset.rideAction=operation;
    button.addEventListener("click",()=>act(operation,statement));row.append(button);
  };
  choose("仅我接受原共乘约定","accept","我本人接受原完整名单、驾驶者、载具、目的地与期限；这次接受不代表登车或替其他人答应。");
  choose("仅我婉拒这次共乘","decline","这次共乘我本人暂不参加；请保留其他成员各自的真实选择。");
  choose("我本人登车","board","我本人现在愿意登上原约定载具；请核对我实际在车边及安全登车条件，不替其他人登车。");
  choose("由我驾驶原定这一程","depart","我作为原驾驶者本人决定开始原定这一程，仍需每位成员已分别同意并实际登车及驾驶权有效。");
  choose("我请求安全靠站","request_stop","我本人希望离开，请在可安全下车的地点靠站；靠站前保留我的实际乘员位置，不在途中直接下车。");
  choose("我本人安全下车","disembark","我本人现在愿意下车，请核对载具已静止并已在安全上落地点；不替其他成员下车。");
  choose("结束原共乘约定","end","我作为原提出者决定结束这份共乘约定，保留各人的真实位置、财物和已经发生的经历。");
  let stage=null;
  if(ride.status==="pending" && !(ride.members||[]).some(member=>member.decision==="declined"))stage="accept";
  else if(["accepted","boarding"].includes(ride.status))stage=(ride.members||[]).some(member=>member.decision==="accepted"&&!member.boarded_at)?"board":"depart";
  const needsReply=stage && (ride.members||[]).some(member=>member.character_id!==context.playerId &&
    (stage==="accept"?member.decision==="pending":stage==="board"?member.decision==="accepted"&&!member.boarded_at:member.character_id===ride.controller_id));
  if(needsReply) {
    const button=document.createElement("button");button.type="button";button.className="button secondary";button.dataset.rideAction="ask_"+stage;
    button.textContent=stage==="accept"?"询问尚未回应成员":stage==="board"?"询问已同意成员是否登车":"询问驾驶者是否出发";
    const questionKey=context.key+":"+context.epoch+":"+rideVersion(ride)+":"+stage;
    button.disabled=rideAsked.has(questionKey);row.append(button);
    button.addEventListener("click",async()=>{
      if(!current()||state.busy||state.responsePending||rideActions.has(context.key)||rideAsked.has(questionKey))return;
      rideActions.add(context.key);rideAsked.add(questionKey);button.disabled=true;
      try {
        await requestRideReplies(naturalSocialContext(),{operation:"ask",ride_id:ride.id,stage},{ride_id:ride.id},"想听听你本人对原共乘安排的想法。",rideVersion(ride));
        if(current())await renderMyRides(true);
      } catch {if(current())notice.textContent="成员回应暂未完成，已有选择和登车状态仍保留。";}
      finally {rideActions.delete(context.key);}
    });
  }
  return row;
}
function appendRideProposal(panel,options,context,isCurrent=()=>true) {
  const form=document.createElement("form");form.className="long-term-form ride-proposal";
  const current=()=>form.isConnected&&rideCurrent(context)&&isCurrent(),draft=rideDrafts.get(context.key)||{};
  const candidates=(options.members||[]).filter(member=>member.id===context.playerId||!member.is_player),vehicles=options.vehicles||[],places=options.places||[];
  if(!candidates.some(member=>member.id===context.playerId))candidates.unshift({id:context.playerId,name:"你",is_player:true});
  const vehicle=rideField(form,"共乘载具","select");vehicle.required=true;
  for(const value of vehicles)rideOption(vehicle,value.id,value.name+" · "+value.capacity+"座（含驾驶者）");
  if(draft.vehicle_id&&!vehicles.some(value=>value.id===draft.vehicle_id))rideOption(vehicle,draft.vehicle_id,"原选载具当前不可用");
  if(draft.vehicle_id)vehicle.value=draft.vehicle_id;
  const capacity=rideText(form,"");
  const memberBox=document.createElement("fieldset");rideText(memberBox,"完整邀请名单（含你和驾驶者）","legend");form.append(memberBox);
  const checks=[],selected=new Set(draft.member_ids||[context.playerId]);
  for(const member of candidates) {
    const check=rideField(memberBox,member.id===context.playerId?"你（本人的共乘提议）":member.name||"眼前成员");check.type="checkbox";check.value=member.id;check.dataset.rideMember=member.id;
    check.checked=member.id===context.playerId||selected.has(member.id);check.disabled=member.id===context.playerId;checks.push(check);
  }
  for(const id of [...selected].filter(id=>!candidates.some(member=>member.id===id))) {
    const check=rideField(memberBox,"原选成员（当前不可邀，可自行取消）");check.type="checkbox";check.value=id;check.checked=true;check.dataset.rideMember=id;checks.push(check);
  }
  const driver=rideField(form,"本次驾驶者","select");driver.required=true;
  const destination=rideField(form,"共乘目的地","select");destination.required=true;
  for(const place of places)rideOption(destination,place.id,place.name);
  if(draft.destination_location_id&&!places.some(place=>place.id===draft.destination_location_id))rideOption(destination,draft.destination_location_id,"原选地点当前不可用");
  if(draft.destination_location_id)destination.value=draft.destination_location_id;
  const deadline=rideField(form,"共乘期限（世界时间 UTC）");deadline.type="datetime-local";deadline.required=true;
  const at=Date.parse(state.living?.world_time||"");deadline.value=draft.deadline||(Number.isFinite(at)?new Date(at+4*3600000).toISOString().slice(0,16):"");
  const speech=rideField(form,"想怎样商量共乘","textarea");speech.required=true;speech.maxLength=120;speech.value=draft.statement||"";
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出共乘，分别听取回应";form.append(submit);
  const notice=rideText(form,"");notice.setAttribute("aria-live","polite");
  const ids=()=>checks.filter(check=>check.checked).map(check=>check.value);
  function save(){if(!rideCurrent(context))return;rideDrafts.set(context.key,{vehicle_id:vehicle.value,controller_id:driver.value,member_ids:ids(),destination_location_id:destination.value,deadline:deadline.value,statement:speech.value});}
  form.rideSaveDraft=save;
  function syncDriver(preferred=driver.value) {
    driver.replaceChildren();const car=vehicles.find(value=>value.id===vehicle.value),controllerIds=car?.controller_ids||[car?.controller_id].filter(Boolean);
    for(const member of candidates.filter(member=>ids().includes(member.id)&&controllerIds.includes(member.id)))rideOption(driver,member.id,member.id===context.playerId?"由我驾驶":(member.name||"原选成员")+"本人决定是否驾驶");
    if(preferred&&controllerIds.includes(preferred)&&ids().includes(preferred))driver.value=preferred;
    capacity.textContent=car?"载具共"+car.capacity+"座，已选"+ids().length+"人；驾驶者也占一座。":"请重新核对可用载具。";
  }
  vehicle.addEventListener("change",()=>{syncDriver();save();});checks.forEach(check=>check.addEventListener("change",()=>{syncDriver();save();}));
  driver.addEventListener("change",save);destination.addEventListener("change",save);deadline.addEventListener("input",save);speech.addEventListener("input",save);syncDriver(draft.controller_id);
  submit.disabled=!vehicles.length||!places.length||candidates.length<2;
  if(!vehicles.length)rideText(form,"附近暂无已确认位置、座位和驾驶权的可用载具。");
  rideText(form,"每个人可分别接受或婉拒；接受后还需本人登车。驾驶者、座位、实际位置与安全靠站条件均以出发时为准。");
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!current()||state.busy||state.responsePending||submit.disabled||rideActions.has(context.key))return;
    if(!form.reportValidity())return;
    const members=ids(),car=vehicles.find(value=>value.id===vehicle.value),end=new Date(deadline.value+"Z");
    if(!car||members.length<2||members.length>car.capacity||!members.includes(driver.value)||!Number.isFinite(end.getTime())){notice.textContent="请核对完整名单、座位数、名单中的有效驾驶者和共乘期限。";return;}
    const statement=speech.value.trim()+" 我提出表内完整名单、载具、驾驶者、目的地与期限；各人分别接受及登车，途中离开先安全靠站。";
    const choice={operation:"propose",vehicle_id:vehicle.value,controller_id:driver.value,member_ids:members,destination_location_id:destination.value,deadline_world_time:end.toISOString(),statement};
    save();rideActions.add(context.key);const inputs=form.querySelectorAll("input, select, textarea, button");inputs.forEach(input=>{input.disabled=true;});
    try {
      const [data,fresh]=await Promise.all([api("/api/worlds/"+context.worldId+"/rides"),api("/api/worlds/"+context.worldId+"/rides/options")]);
      if(!current())return;
      if(rideRows(data).some(value=>rideLive.has(value.status))){notice.textContent="你已有尚未结束的共乘，请先处理原约定；草稿已保留。";return;}
      const latestCar=(fresh.vehicles||[]).find(value=>value.id===choice.vehicle_id),knownIds=new Set((fresh.members||[]).map(member=>member.id));
      if(fresh.player_id!==context.playerId||!latestCar||members.length>latestCar.capacity||members.some(id=>!knownIds.has(id))||
        !(latestCar.controller_ids||[latestCar.controller_id]).includes(choice.controller_id)||!(fresh.places||[]).some(place=>place.id===choice.destination_location_id)) {
        notice.textContent="载具、驾驶权、成员或目的地已变化，请刷新后核对；完整草稿已保留。";return;
      }
      const result=await submitRide(context,members.find(id=>id!==context.playerId),choice);if(!current())return;
      if(result?.event_id){rideDrafts.delete(context.key);form.rideSaveDraft=null;await renderMyRides(true);}
      else notice.textContent="提议尚未确认保存，原话和完整草稿已保留。";
    } catch {if(current())notice.textContent="暂时无法保存共乘提议，原话和完整草稿已保留。";}
    finally {rideActions.delete(context.key);if(current()){inputs.forEach(input=>{input.disabled=false;});checks.find(check=>check.value===context.playerId).disabled=true;submit.disabled=!vehicles.length||!places.length||candidates.length<2;}}
  });
  panel.append(form);return form;
}
async function renderMyRides(force=false) {
  const context=rideContext();
  if(!context.key||!state.living){if(rideRead)rideRead.invalidated=true;document.querySelector("#my-rides")?.remove();rideReadAt=0;return;}
  let panel=document.querySelector("#my-rides");
  if(!panel){const anchor=document.querySelector("#my-group-journeys")||document.querySelector("#my-journeys")||elements.sceneCharacters;if(!anchor)return;panel=document.createElement("section");panel.id="my-rides";panel.setAttribute("aria-labelledby","my-rides-heading");anchor.after(panel);}
  const token=context.key+":"+context.epoch;
  if(panel.dataset.rideContext!==token){
    panel.querySelector(".ride-proposal")?.rideSaveDraft?.();panel.dataset.rideContext=token;panel.replaceChildren();rideText(panel,"我的共乘","h3").id="my-rides-heading";
    const refresh=document.createElement("button");refresh.type="button";refresh.className="button secondary";refresh.textContent="刷新共乘与可用载具";
    refresh.addEventListener("click",()=>{if(!state.busy&&!state.responsePending&&!rideActions.has(context.key))return renderMyRides(true);});panel.append(refresh);
    const list=document.createElement("div");list.className="ride-list";rideText(list,"正在读取本人共乘记录……");panel.append(list);
    const proposal=document.createElement("details");proposal.className="ride-proposal-box";rideText(proposal,"商量一次新的有限共乘","summary");panel.append(proposal);
  }
  if(!force&&(state.busy||state.responsePending||rideActions.has(context.key)||rideRead&&!rideRead.invalidated||Date.now()-rideReadAt<4000))return;
  if(rideRead)rideRead.invalidated=true;const request={context,invalidated:false};rideRead=request;
  const current=()=>rideRead===request&&!request.invalidated&&panel.isConnected&&panel.dataset.rideContext===token&&rideCurrent(context);
  try {
    const [data,options]=await Promise.all([api("/api/worlds/"+context.worldId+"/rides"),api("/api/worlds/"+context.worldId+"/rides/options")]);
    if(!current())return;rideReadAt=Date.now();panel.querySelector(".ride-read-error")?.remove();
    const rows=rideRows(data),shown=[...rows.filter(value=>rideLive.has(value.status)),...rows.filter(value=>!rideLive.has(value.status)).slice(0,2)],list=panel.querySelector(".ride-list"),proposal=panel.querySelector(".ride-proposal-box");
    const signature=JSON.stringify(shown);if(list.dataset.signature!==signature){list.querySelectorAll("form").forEach(form=>{form.rideRecoverySaveDraft?.();form.rideHandoffSaveDraft?.();});list.dataset.signature=signature;list.replaceChildren();if(!shown.length)rideText(list,"目前没有已记录的共乘。");for(const value of shown)list.append(rideRow(value,context,()=>panel.isConnected&&panel.dataset.rideContext===token));}
    const optionSignature=JSON.stringify(options);if(proposal.dataset.signature!==optionSignature||!proposal.querySelector("form")?.rideSaveDraft){proposal.querySelector("form")?.rideSaveDraft?.();proposal.dataset.signature=optionSignature;proposal.replaceChildren();rideText(proposal,"商量一次新的有限共乘","summary");appendRideProposal(proposal,options,context,()=>panel.isConnected&&panel.dataset.rideContext===token);}
  } catch {if(current()&&!panel.querySelector(".ride-read-error"))rideText(panel,"暂时无法刷新共乘，现有记录与草稿已保留。").className="ride-read-error muted";}
  finally {if(rideRead===request)rideRead=null;}
}
const ridesOriginalLiving=renderLiving;
renderLiving=function(){ridesOriginalLiving();renderMyRides();};
