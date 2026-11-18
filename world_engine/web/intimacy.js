/* 哥哥，页面只能提交玩家自己的选择；对方通过真实对话决定是否回应。 */
Object.assign(eventLabels, {
  "social.intimacy_propose": "表达关系心意", "social.intimacy_accept": "双方确认关系",
  "social.intimacy_decline": "关系提议被拒绝", "social.intimacy_end": "关系约定结束",
  "social.intimacy_boundary": "表达相处界限",
  "social.shared_invited": "共同生活邀请", "social.shared_started": "共同生活开始",
  "social.shared_completed": "共同生活完成", "social.shared_interrupted": "共同生活中止",
  "social.shared_declined": "共同生活邀请被拒绝", "social.shared_cancelled": "共同生活邀请撤回",
});
const intimacyStatus = {pending: "等待回应", active: "双方已确认", declined: "已拒绝", ended: "已结束", withdrawn: "已撤回", expired: "提议已过期"};

let naturalSocialContextKey = "", naturalSocialContextEpoch = 0;
function naturalSocialContext() {
  const worldId = state.worldId, playerId = typeof currentPlayer === "function" ? currentPlayer()?.id : null;
  const key = String(worldId || "") + ":" + String(playerId || "");
  if (key !== naturalSocialContextKey) { naturalSocialContextKey = key; naturalSocialContextEpoch += 1; }
  return {worldId,playerId,key,epoch:naturalSocialContextEpoch};
}
function naturalSocialCurrent(context) {
  const current = naturalSocialContext();
  return current.key === context.key && current.epoch === context.epoch;
}
if (typeof renderLiving === "function") {
  const naturalSocialRenderLiving = renderLiving;
  renderLiving = function(...args) { naturalSocialContext(); return naturalSocialRenderLiving.apply(this,args); };
}

async function requestGroupJourneyReplies(context, choice, saved, intent) {
  const restore = choice.operation === "resume";
  if (!(choice.operation === "propose" || restore && saved.status === "resume_pending")) return;
  const path = `/api/worlds/${context.worldId}/group-journeys`;
  const rows = await api(path);
  if (!naturalSocialCurrent(context)) return;
  const original = rows.find(row => row.id === saved.journey_id);
  if (!original) return;
  const contract = row => JSON.stringify([row.id,row.proposer_id,row.leader_id,row.player_id,row.title,
    row.statement,row.origin_id,row.deadline_at,row.separation_policy,row.return_to_origin,row.resume_epoch,
    (row.members || []).map(member => member.character_id),
    (row.stops || []).map(stop => [stop.stop_index,stop.location_id,stop.minutes,stop.is_return])]);
  const frozen = contract(original);
  const memberIds = (original.members || []).map(member => member.character_id).filter(id => id !== context.playerId);
  for (const memberId of memberIds) {
    if (!naturalSocialCurrent(context)) return;
    const currentRows = await api(path);
    if (!naturalSocialCurrent(context)) return;
    const trip = currentRows.find(row => row.id === saved.journey_id);
    if (!trip || contract(trip) !== frozen || trip.status !== (restore ? "resume_pending" : "pending")) return;
    const member = (trip.members || []).find(row => row.character_id === memberId);
    if (!member || member.left_at || (restore ? member.resumed || member.decision !== "accepted" : member.decision !== "pending")) continue;
    const options = await api(path + "/options");
    if (!naturalSocialCurrent(context)) return;
    if (!(options.members || []).some(row => row.id === memberId && !row.is_player)) continue;
    // 哥哥，逐人发问只等待本人的真实回应，远处不借信笺补出新的当面选择。
    let responseFinished = false;
    await perform("正在分别询问成员本人的想法", async () => {
      if (!naturalSocialCurrent(context)) return {};
      const words = restore ? "对于刚才恢复原有限多人行程的请求，你本人现在愿意恢复吗？其他成员会各自决定。"
        : "对于刚才这份完整多人行程，你本人愿意接受还是婉拒？其他成员会各自决定。";
      let result = await api(`/api/worlds/${context.worldId}/player/act`, {
        method:"POST", body:JSON.stringify({intent:"说话：" + intent + "\n" + words,target_character_id:memberId}),
      });
      if (!naturalSocialCurrent(context)) return result;
      if (result.npc_reply_pending && result.outcome?.event_id) {
        for (let attempt = 0; attempt < 40 && naturalSocialCurrent(context); attempt += 1) {
          const reaction = await api(`/api/worlds/${context.worldId}/player/actions/${result.outcome.event_id}/reaction`);
          if (!naturalSocialCurrent(context)) return result;
          if (reaction.status !== "pending") {
            result = {...result,npc_reply_pending:false,npc_reply:reaction.reply,
              npc_stage_direction:reaction.stage_direction,npc_reply_error:reaction.error};
            break;
          }
          await new Promise(resolve => window.setTimeout(resolve,1500));
        }
      }
      if (naturalSocialCurrent(context)) {
        if (result.outcome && typeof renderActionResult === "function") renderActionResult(result.outcome,result);
        else if (result.npc_reply) showActionResult(result.npc_reply);
      }
      responseFinished = !result.npc_reply_pending;
      return result;
    }, "成员回应状态已更新", null, {inline:true,continuing:true});
    if (!responseFinished) return;
  }
}

const rideRecoveryReplyAttempts = new Set();
function rideRecoveryReplyKey(context,ride,stage,memberId) {
  return JSON.stringify([context.key,ride.id,ride.recovery?.id,ride.recovery?.epoch,stage,memberId]);
}
function rideRecoveryHasPendingReplies(ride,stage,context=naturalSocialContext()) {
  const recovery=ride.recovery;
  if(!recovery||ride.status!=="blocked"||!["accept_recovery","resume"].includes(stage))return false;
  if(stage==="accept_recovery"&&recovery.status!=="pending"||stage==="resume"&&recovery.status!=="accepted")return false;
  return (recovery.members||[]).some(member=>member.character_id!==context.playerId&&
    !rideRecoveryReplyAttempts.has(rideRecoveryReplyKey(context,ride,stage,member.character_id))&&
    (stage==="accept_recovery"?member.decision==="pending":member.character_id===(recovery.controller_id||ride.effective_controller_id||ride.controller_id)&&member.decision==="accepted"));
}
async function requestRideRecoveryReplies(context,choice,intent,expectedVersion=null) {
  if(choice.operation!=="ask"||!["accept_recovery","resume"].includes(choice.stage))return;
  const path=`/api/worlds/${context.worldId}/rides`,unwrap=value=>Array.isArray(value)?value:value?.rides||[];
  const original=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id);
  if(!naturalSocialCurrent(context)||!original?.recovery||original.recovery.id!==choice.recovery_id||
      expectedVersion&&rideRecoveryVersion(original)!==expectedVersion)return;
  const frozen=rideRecoveryContract(original),stage=choice.stage;
  const memberIds=(original.recovery.members||[]).filter(member=>member.character_id!==context.playerId&&
    (stage!=="resume"||member.character_id===(original.recovery.controller_id||original.effective_controller_id||original.controller_id))).map(member=>member.character_id);
  for(const memberId of memberIds) {
    if(!naturalSocialCurrent(context))return;
    const trip=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id),recovery=trip?.recovery;
    if(!naturalSocialCurrent(context)||!recovery||recovery.id!==choice.recovery_id||rideRecoveryContract(trip)!==frozen||trip.status!=="blocked"||
        recovery.status!==(stage==="accept_recovery"?"pending":"accepted")||
        (trip.members||[]).some(member=>!member.boarded_at||member.disembarked_at)||
        (recovery.members||[]).some(member=>member.decision==="declined"))return;
    const member=(recovery.members||[]).find(person=>person.character_id===memberId);
    if(!member||stage==="accept_recovery"&&member.decision!=="pending")continue;
    if(stage==="resume"&&(!(recovery.members||[]).every(person=>person.decision==="accepted")||memberId!==(recovery.controller_id||trip.effective_controller_id||trip.controller_id)))return;
    const key=rideRecoveryReplyKey(context,trip,stage,memberId);if(rideRecoveryReplyAttempts.has(key))continue;
    const options=await api(path+"/options");
    if(!naturalSocialCurrent(context))return;
    if(!(options.members||[]).some(person=>person.id===memberId&&!person.is_player))continue;
    // 哥哥，询问前再核对停留轮次；候选读取期间也可能有人退出或重新挂起。
    const ready=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id);
    if(!naturalSocialCurrent(context)||!ready?.recovery||rideRecoveryContract(ready)!==frozen||ready.status!=="blocked"||
        ready.recovery.status!==(stage==="accept_recovery"?"pending":"accepted")||
        (ready.members||[]).some(person=>!person.boarded_at||person.disembarked_at))return;
    const readyMember=(ready.recovery.members||[]).find(person=>person.character_id===memberId);
    if(!readyMember||stage==="accept_recovery"&&readyMember.decision!=="pending")continue;
    if(stage==="resume"&&!(ready.recovery.members||[]).every(person=>person.decision==="accepted"))return;
    let responseFinished=false;
    await perform("正在询问成员本人的续程决定",async()=>{
      if(!naturalSocialCurrent(context))return {};
      rideRecoveryReplyAttempts.add(key);
      const question=stage==="accept_recovery"?
        "对于眼前这份原驾驶者、原在车名单、原路线剩余路程和新期限都明确的续程方案，你本人愿意同意还是婉拒？这不代表恢复驾驶，原安全靠站请求仍保留。":
        "大家已经分别同意这份续程方案。你作为原驾驶者，现在本人是否选择继续原路线？仍须真实驾驶权、身体、期限及全部在车授权有效，原安全靠站请求仍保留。";
      let result=await api(`/api/worlds/${context.worldId}/player/act`,{method:"POST",body:JSON.stringify({intent:"说话："+intent+"\n"+question,target_character_id:memberId})});
      if(!naturalSocialCurrent(context))return result;
      if(result.npc_reply_pending&&result.outcome?.event_id) {
        for(let attempt=0;attempt<40&&naturalSocialCurrent(context);attempt+=1) {
          const reaction=await api(`/api/worlds/${context.worldId}/player/actions/${result.outcome.event_id}/reaction`);
          if(!naturalSocialCurrent(context))return result;
          if(reaction.status!=="pending") {result={...result,npc_reply_pending:false,npc_reply:reaction.reply,npc_stage_direction:reaction.stage_direction,npc_reply_error:reaction.error};break;}
          await new Promise(resolve=>window.setTimeout(resolve,1500));
        }
      }
      if(naturalSocialCurrent(context)) {
        if(result.outcome&&typeof renderActionResult==="function")renderActionResult(result.outcome,result);
        else if(result.npc_reply)showActionResult(result.npc_reply);
      }
      responseFinished=!result.npc_reply_pending&&!result.npc_reply_error&&typeof result.npc_reply==="string"&&Boolean(result.npc_reply.trim());return result;
    },"成员本人的续程回应已更新",null,{inline:true,continuing:true});
    if(!responseFinished||!naturalSocialCurrent(context))return;
    const after=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id);
    if(!naturalSocialCurrent(context)||!after?.recovery||rideRecoveryContract(after)!==frozen)return;
    const answered=(after.recovery.members||[]).find(person=>person.character_id===memberId);
    // 哥哥，真实台词没有形成结构化同意时保留pending，不自动继续付费询问。
    if(stage==="accept_recovery"&&answered?.decision!=="accepted")return;
  }
}
const rideHandoffReplyAttempts = new Set();
function rideHandoffReplyKey(context,ride,stage,memberId) {
  return JSON.stringify([context.key,ride.id,ride.handoff?.id,ride.handoff?.epoch,stage,memberId]);
}
function rideHandoffStageReady(ride,stage) {
  const handoff=ride.handoff,authority=handoff?.authority||{};
  if(!handoff||!rideHandoffRosterCurrent(ride)||ride.status!=="blocked"||!["pending","accepted"].includes(handoff.status)||
      (ride.members||[]).some(member=>!member.boarded_at||member.disembarked_at)||
      (handoff.members||[]).some(member=>member.decision==="declined"))return false;
  if(stage==="accept_handoff")return handoff.status==="pending";
  if(stage==="authorize_handoff")return Boolean(authority.holder_id)&&!authority.grant_event_id&&!authority.revoke_event_id;
  if(stage==="take_control")return handoff.status==="accepted"&&(handoff.members||[]).every(member=>member.decision==="accepted")&&Boolean(authority.grant_event_id)&&!authority.revoke_event_id;
  return false;
}
function rideHandoffTargets(ride,stage,playerId) {
  const handoff=ride.handoff,authority=handoff?.authority||{},driver=handoff?.terms?.controller_id||handoff?.controller_id;
  return (handoff?.members||[]).filter(member=>member.character_id!==playerId&&
    (stage==="accept_handoff"?member.decision==="pending":stage==="authorize_handoff"?member.character_id===authority.holder_id:member.character_id===driver)).map(member=>member.character_id);
}
function rideHandoffHasPendingReplies(ride,stage,context=naturalSocialContext()) {
  return rideHandoffStageReady(ride,stage)&&rideHandoffTargets(ride,stage,context.playerId).some(id=>!rideHandoffReplyAttempts.has(rideHandoffReplyKey(context,ride,stage,id)));
}
async function requestRideHandoffReplies(context,choice,intent,expectedVersion=null) {
  if(choice.operation!=="ask"||!["accept_handoff","authorize_handoff","take_control"].includes(choice.stage))return;
  const path=`/api/worlds/${context.worldId}/rides`,unwrap=value=>Array.isArray(value)?value:value?.rides||[];
  const original=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id),stage=choice.stage;
  if(!naturalSocialCurrent(context)||!original?.handoff||original.handoff.id!==choice.handoff_id||
      expectedVersion&&rideHandoffVersion(original)!==expectedVersion||!rideHandoffStageReady(original,stage))return;
  const frozen=rideHandoffContract(original),memberIds=rideHandoffTargets(original,stage,context.playerId);
  for(const memberId of memberIds) {
    if(!naturalSocialCurrent(context))return;
    const trip=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id);
    if(!naturalSocialCurrent(context)||!trip?.handoff||trip.handoff.id!==choice.handoff_id||rideHandoffContract(trip)!==frozen||!rideHandoffStageReady(trip,stage))return;
    if(!rideHandoffTargets(trip,stage,context.playerId).includes(memberId))continue;
    const key=rideHandoffReplyKey(context,trip,stage,memberId);if(rideHandoffReplyAttempts.has(key))continue;
    const options=await api(path+"/options");
    if(!naturalSocialCurrent(context))return;
    if(!(options.members||[]).some(person=>person.id===memberId&&!person.is_player))continue;
    // 哥哥，候选读取和付费请求之间再次核验，不能把已失效的权利或轮次交给旧回包。
    const ready=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id);
    if(!naturalSocialCurrent(context)||!ready?.handoff||rideHandoffContract(ready)!==frozen||!rideHandoffStageReady(ready,stage))return;
    if(!rideHandoffTargets(ready,stage,context.playerId).includes(memberId))continue;
    let responseFinished=false;
    await perform("正在询问成员本人的交接决定",async()=>{
      if(!naturalSocialCurrent(context))return {};
      rideHandoffReplyAttempts.add(key);
      const question=stage==="accept_handoff"?
        "对于眼前这份接驾驶者、完整原在车名单、原路线余程和新期限都明确的交接方案，你本人愿意同意还是婉拒？这不授予驾驶许可，也不代表接管或继续行驶；原安全靠站请求仍保留。":stage==="authorize_handoff"?
        "你作为当前合法持有人，是否本人只对眼前这份方案、接驾驶者、原路线和新期限给予有限驾驶许可？这是另一次独立授权，不转财产、租约或费用，不代全员同意，也不自动开车。":
        "大家已经分别同意，合法持有人也已另行授予本次驾驶许可。你作为选定的接驾驶者，现在本人是否选择接管并继续原路线？仍须实际身体、权限和期限满足，原安全靠站请求保留。";
      let result=await api(`/api/worlds/${context.worldId}/player/act`,{method:"POST",body:JSON.stringify({intent:"说话："+intent+"\n"+question,target_character_id:memberId})});
      if(!naturalSocialCurrent(context))return result;
      if(result.npc_reply_pending&&result.outcome?.event_id) {
        for(let attempt=0;attempt<40&&naturalSocialCurrent(context);attempt+=1) {
          const reaction=await api(`/api/worlds/${context.worldId}/player/actions/${result.outcome.event_id}/reaction`);
          if(!naturalSocialCurrent(context))return result;
          if(reaction.status!=="pending") {result={...result,npc_reply_pending:false,npc_reply:reaction.reply,npc_stage_direction:reaction.stage_direction,npc_reply_error:reaction.error};break;}
          await new Promise(resolve=>window.setTimeout(resolve,1500));
        }
      }
      if(naturalSocialCurrent(context)) {
        if(result.outcome&&typeof renderActionResult==="function")renderActionResult(result.outcome,result);
        else if(result.npc_reply)showActionResult(result.npc_reply);
      }
      responseFinished=!result.npc_reply_pending&&!result.npc_reply_error&&typeof result.npc_reply==="string"&&Boolean(result.npc_reply.trim());return result;
    },"成员本人的交接回应已更新",null,{inline:true,continuing:true});
    if(!responseFinished||!naturalSocialCurrent(context))return;
    const after=unwrap(await api(path)).find(ride=>ride.id===choice.ride_id);
    if(!naturalSocialCurrent(context)||!after?.handoff||rideHandoffContract(after)!==frozen)return;
    if(stage==="accept_handoff"&&(after.handoff.members||[]).find(member=>member.character_id===memberId)?.decision!=="accepted")return;
    if(stage==="authorize_handoff"&&(!after.handoff.authority?.grant_event_id||after.handoff.authority?.revoke_event_id))return;
  }
}
async function requestRideReplies(context, choice, saved, intent, expectedVersion = null) {
  const stage = choice.operation === "propose" ? "accept" : choice.stage;
  if (!(choice.operation === "propose" || choice.operation === "ask") || !["accept","board","depart"].includes(stage)) return;
  const path = `/api/worlds/${context.worldId}/rides`, rideId = saved.ride_id || choice.ride_id;
  const unwrap = value => Array.isArray(value) ? value : value?.rides || [];
  const rows = unwrap(await api(path));
  if (!naturalSocialCurrent(context)) return;
  const original = rows.find(row => row.id === rideId);
  if (!original || expectedVersion && typeof rideVersion === "function" && rideVersion(original) !== expectedVersion) return;
  const contract = row => JSON.stringify([row.id,row.proposer_id,row.controller_id,row.vehicle_id,row.destination_location_id,row.deadline_at,row.statement,
    (row.members || []).map(member => member.character_id)]);
  const frozen = contract(original);
  const memberIds = (original.members || []).map(member => member.character_id).filter(id => id !== context.playerId && (stage !== "depart" || id === original.controller_id));
  for (const memberId of memberIds) {
    if (!naturalSocialCurrent(context)) return;
    const trip = unwrap(await api(path)).find(row => row.id === rideId);
    if (!naturalSocialCurrent(context)) return;
    const permitted = stage === "accept" ? ["pending"] : ["accepted","boarding"];
    if (!trip || contract(trip) !== frozen || !permitted.includes(trip.status) || (trip.members || []).some(member => member.decision === "declined")) return;
    const member = (trip.members || []).find(row => row.character_id === memberId);
    if (!member || member.disembarked_at || (stage === "accept" ? member.decision !== "pending" : member.decision !== "accepted" || stage === "board" && member.boarded_at)) continue;
    if (stage === "depart" && !(trip.members || []).every(person => person.decision === "accepted" && person.boarded_at && !person.disembarked_at)) return;
    const options = await api(path + "/options");
    if (!naturalSocialCurrent(context)) return;
    if (!(options.members || []).some(person => person.id === memberId && !person.is_player)) continue;
    let responseFinished = false;
    await perform("正在询问成员本人的共乘选择", async () => {
      if (!naturalSocialCurrent(context)) return {};
      const question = stage === "accept" ? "对于这份载具、驾驶者、完整名单、目的地与期限都固定的共乘约定，你本人愿意接受还是婉拒？接受不代表登车。" :
        stage === "board" ? "你本人现在愿意登上原约定的这辆载具吗？这是另一次登车决定，可以保留原地，不代其他人登车。" :
        "你作为原驾驶者，现在本人愿意开始原定这一程吗？仍需核对真实驾驶权和每个人已分别登车。";
      let result = await api(`/api/worlds/${context.worldId}/player/act`, {method:"POST",body:JSON.stringify({intent:"说话：" + intent + "\n" + question,target_character_id:memberId})});
      if (!naturalSocialCurrent(context)) return result;
      if (result.npc_reply_pending && result.outcome?.event_id) {
        for (let attempt = 0; attempt < 40 && naturalSocialCurrent(context); attempt += 1) {
          const reaction = await api(`/api/worlds/${context.worldId}/player/actions/${result.outcome.event_id}/reaction`);
          if (!naturalSocialCurrent(context)) return result;
          if (reaction.status !== "pending") {
            result = {...result,npc_reply_pending:false,npc_reply:reaction.reply,npc_stage_direction:reaction.stage_direction,npc_reply_error:reaction.error};break;
          }
          await new Promise(resolve => window.setTimeout(resolve,1500));
        }
      }
      if (naturalSocialCurrent(context)) {
        if (result.outcome && typeof renderActionResult === "function") renderActionResult(result.outcome,result);
        else if (result.npc_reply) showActionResult(result.npc_reply);
      }
      responseFinished = !result.npc_reply_pending;return result;
    },"成员本人的回应状态已更新",null,{inline:true,continuing:true});
    if (!responseFinished) return;
  }
}

async function executeNaturalSocialChoice(intent, preview, replyChannel = null) {
  let applied = false;
  let appliedResult = null;
  const context = naturalSocialContext(), draft = elements.playerIntent.value;
  const choicePayload = {recipient_id: preview.target_character_id, intent, evidence: preview.choice_evidence,
    relationship_choice: preview.relationship_choice, shared_life_choice: preview.shared_life_choice,
    family_choice: preview.family_choice, appointment_choice: preview.appointment_choice, shared_plan_choice: preview.shared_plan_choice,journey_choice:preview.journey_choice,
    overnight_choice: preview.overnight_choice, separate_stay_choice: preview.separate_stay_choice,
    lodging_plan_choice: preview.lodging_plan_choice, companion_choice: preview.companion_choice};
  if (preview.group_journey_choice) choicePayload.group_journey_choice = preview.group_journey_choice;
  if (preview.ride_choice) choicePayload.ride_choice = preview.ride_choice;
  const signature = JSON.stringify([context.key,choicePayload]);
  if (state.pendingSocialChoice?.signature !== signature) state.pendingSocialChoice = {signature, requestId: crypto.randomUUID()};
  const request = state.pendingSocialChoice;
  await perform("正在保存你明确表达的选择", async () => {
    const result = await api(`/api/worlds/${context.worldId}/player/social-choices`, {
      method: "POST", body: JSON.stringify({request_id: request.requestId, ...choicePayload}),
    });
    applied = true; appliedResult = result;
    if (naturalSocialCurrent(context)) {
      if (state.pendingSocialChoice === request) state.pendingSocialChoice = null;
      if (elements.playerIntent.value === draft) { elements.playerIntent.value = ""; saveIntentDraft(); }
      showActionResult(result.summary); finishResponse(result.summary);
    }
    return result;
  }, "你的选择已保存");
  if (preview.ride_choice) {
    if (applied && naturalSocialCurrent(context)) {
      try { await requestRideReplies(context,preview.ride_choice,appliedResult,intent); }
      catch { if (naturalSocialCurrent(context)) showToast("你的共乘选择已保存；成员回应暂未完成，可以稍后继续商量。",true); }
    }
    return appliedResult;
  }
  if (preview.group_journey_choice) {
    if (applied && naturalSocialCurrent(context)) {
      try { await requestGroupJourneyReplies(context,preview.group_journey_choice,appliedResult,intent); }
      catch (error) {
        if (naturalSocialCurrent(context)) showToast("你的选择已保存；成员回应暂未完成，可以稍后继续商量。",true);
      }
    }
    return appliedResult;
  }
  if (applied && (!preview.shared_life_choice || preview.shared_life_choice.operation === "invite") &&
      (!preview.overnight_choice || ["propose", "accept", "decline"].includes(preview.overnight_choice.operation)) &&
      (!preview.separate_stay_choice || ["propose", "accept", "decline", "reschedule"].includes(preview.separate_stay_choice.operation)) &&
      (!preview.lodging_plan_choice || ["propose", "accept", "decline"].includes(preview.lodging_plan_choice.operation)) &&
      (!preview.companion_choice || ["propose", "counter"].includes(preview.companion_choice.operation)) &&
      (!preview.journey_choice || preview.journey_choice.operation==="propose" || preview.journey_choice.operation==="resume" && appliedResult?.status==="resume_pending") &&
      (!preview.shared_plan_choice || preview.shared_plan_choice.operation === "propose") &&
      (!preview.appointment_choice || ["propose", "reschedule"].includes(preview.appointment_choice.operation) ||
       preview.appointment_choice.operation === "resume_series" && appliedResult?.status === "resume_pending")) {
    if (replyChannel === "letter") {
      await perform("正在通过信笺商量安排", async () => {
        const response = await api(`/api/worlds/${state.worldId}/messages`, {
          method: "POST", body: JSON.stringify({recipient_id: preview.target_character_id, content: intent}),
        });
        showActionResult(response.reply); return response;
      }, "信笺已得到回应");
    } else await executePlayerIntent(intent, preview.target_character_id, true);
  }
  return appliedResult;
}

async function submitSharedLife(characterId, choice, talk = false) {
  elements.characterDialog.close();
  return executeNaturalSocialChoice(choice.statement,{target_character_id:characterId,choice_evidence:choice.statement,shared_life_choice:choice});
}

function mealSupplyText(moment,playerId) {
  if(moment.kind!=="meal")return "";
  return moment.meal_supply==="proposer"?`${moment.meal_provider_id===playerId?"你":"对方"}提供双方各一份食物`:"双方各自准备一份食物";
}

function appendMealSupplyField(form,kind,ariaLabel="供餐安排") {
  const label=document.createElement("label");label.textContent=ariaLabel;
  const supply=document.createElement("select");supply.setAttribute("aria-label",ariaLabel);
  for(const [value,text] of [["each_own","各吃一份自己的食物"],["proposer","我提供双方各一份食物，请对方一起吃"]]) {
    const option=document.createElement("option");option.value=value;option.textContent=text;supply.append(option);
  }
  const update=()=>{supply.disabled=kind.value!=="meal";if(supply.disabled)supply.value="each_own";};kind.addEventListener("change",update);update();
  label.append(supply);form.append(label);return supply;
}

async function submitRelationship(characterId, choice, talk = false) {
  let applied = false;
  elements.characterDialog.close();
  await perform("正在记录你的关系选择", async () => {
    const result = await api(`/api/worlds/${state.worldId}/relationships`, {
      method: "POST", body: JSON.stringify({recipient_id: characterId, choice}),
    });
    applied = true;
    showActionResult(result.summary);
    return result;
  }, "你的选择已记录");
  if (applied && talk) {
    // 哥哥，模型失败也不重复提交心意；待回应提议会留在人物档案中。
    if (talk === "letter") {
      await perform("正在寄出你的心意", async () => {
        const response = await api(`/api/worlds/${state.worldId}/messages`, {
          method: "POST", body: JSON.stringify({recipient_id: characterId, content: choice.statement}),
        });
        showActionResult(response.reply || "信笺已得到回应"); return response;
      }, "信笺已得到回应");
    } else await executePlayerIntent(choice.statement, characterId, true);
  }
}

const intimacyOriginalProfile = openCharacterProfile;
openCharacterProfile = async function(characterId) {
  await intimacyOriginalProfile(characterId);
  appendIntimacyProfile(characterId);
};

function appendIntimacyProfile(characterId) {
  if (document.querySelector("#intimacy-profile")) return;
  const character = characterById(characterId);
  const player = currentPlayer();
  if (!character || character.is_player || !player) return;
  const panel = document.createElement("section"); panel.id = "intimacy-profile";
  const heading = document.createElement("h2"); heading.textContent = "彼此的关系"; panel.append(heading);
  const note = document.createElement("p");
  note.textContent = "从共同经历中慢慢了解彼此。心意需要双方明确回应，每个人都可以保留界限或退出。";
  panel.append(note);
  const bonds = (state.living?.intimate_bonds || []).filter((bond) => bond.other_id === characterId);
  for (const bond of bonds) {
    const row = document.createElement("div"); row.className = "life-item";
    const text = document.createElement("p");
    text.textContent = `${bond.label} · ${intimacyStatus[bond.status]}：${bond.statement}${bond.response ? ` / ${bond.response}` : ""}`;
    row.append(text);
    const addChoice = (label, operation, statement) => {
      const button = document.createElement("button"); button.type = "button";
      button.className = "button secondary"; button.textContent = label;
      button.addEventListener("click", () => submitRelationship(characterId, {operation, bond_id: bond.id, statement}));
      row.append(button);
    };
    if (bond.status === "pending") {
      if (bond.proposer_id !== player.id) {
        addChoice(`我愿意成为${bond.label}`, "accept", `我也愿意和你成为${bond.label}，尊重彼此的生活和界限。`);
        addChoice("婉拒这份心意", "decline", "谢谢你告诉我，但我现在不愿意确认这段关系。");
      } else {
        addChoice("撤回我的提议", "end", "我想先撤回这份提议，留给彼此一些空间。");
        const talk = document.createElement("button"); talk.type = "button"; talk.className = "button secondary";
        talk.textContent = "继续谈谈这份心意";
        talk.addEventListener("click", () => { elements.characterDialog.close(); fillIntent("你对我们之间的关系有什么想法？可以坦白告诉我。", characterId); });
        row.append(talk);
      }
    } else if (bond.status === "active") {
      addChoice("结束这份关系约定", "end", "我决定结束这份关系约定。过去的经历仍然保留，请尊重我的选择。");
    }
    panel.append(row);
  }
  const boundaries = (state.living?.relationship_boundaries || []).filter((item) =>
    item.character_id === characterId && item.other_id === player.id || item.character_id === player.id && item.other_id === characterId);
  for (const item of boundaries) {
    const text = document.createElement("p");
    text.textContent = `${item.character_id === player.id ? "你" : "对方"}表达的界限：${item.statement}${item.pause_contact ? "（暂停主动联系）" : ""}`;
    panel.append(text);
  }
  const form = document.createElement("form"); form.className = "long-term-form";
  const reachable = canInviteCompanion(character) && distanceKm(player.longitude, player.latitude, character.longitude, character.latitude) <= 0.02;
  const contact = state.contacts.some((item) => item.recipient_id === characterId);
  if (!reachable && !contact) {
    const distanceNote = document.createElement("p");
    distanceNote.textContent = "当面表达心意请先走近到20米内；远程相处需要先交换联络信笺。";
    panel.append(distanceNote);
  }
  const kindLabel = document.createElement("label"); kindLabel.textContent = "希望如何相处";
  const kind = document.createElement("select"); kind.setAttribute("aria-label", "希望如何相处");
  for (const [value, label] of [["close_friend", "成为挚友"], ["romantic", "成为恋人"]]) {
    const option = document.createElement("option"); option.value = value; option.textContent = label; kind.append(option);
  }
  kindLabel.append(kind); form.append(kindLabel);
  const statementLabel = document.createElement("label"); statementLabel.textContent = "想对对方说的话";
  const statement = document.createElement("textarea"); statement.maxLength = 240; statement.required = true;
  statement.setAttribute("aria-label", "想对对方说的话"); statement.placeholder = "说说你的心意，也给对方自由回应的空间……";
  statementLabel.append(statement); form.append(statementLabel);
  const button = document.createElement("button"); button.type = "submit"; button.className = "button primary"; button.textContent = "表达心意并听听回应";
  button.disabled = !reachable && !contact;
  form.append(button);
  form.addEventListener("submit", (event) => {
    event.preventDefault(); if (!statement.value.trim()) return;
    const nearby = canInviteCompanion(character) && distanceKm(player.longitude, player.latitude, character.longitude, character.latitude) <= 0.02;
    submitRelationship(characterId, {operation: "propose", kind: kind.value, statement: statement.value.trim()}, nearby ? true : "letter");
  });
  panel.append(form);
  const boundaryForm = document.createElement("div"); boundaryForm.className = "long-term-form";
  const boundaryLabel = document.createElement("label"); boundaryLabel.textContent = "我的相处界限";
  const boundary = document.createElement("textarea"); boundary.maxLength = 240; boundary.setAttribute("aria-label", "我的相处界限");
  boundary.placeholder = "例如：我想慢慢了解彼此，暂时不要主动来找我。";
  boundaryLabel.append(boundary); boundaryForm.append(boundaryLabel);
  for (const [pause, label] of [[true, "表达界限并暂停主动联系"], [false, "表达界限并允许主动联系"]]) {
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = label;
    button.addEventListener("click", () => {
      if (!boundary.value.trim()) return showToast("先写下你希望如何相处", true);
      submitRelationship(characterId, {operation: "boundary", statement: boundary.value.trim(), pause_contact: pause});
    }); boundaryForm.append(button);
  }
  panel.append(boundaryForm);
  elements.characterProfile.append(panel);
  appendSharedLifeProfile(characterId, player, reachable);
}

function appendSharedLifeProfile(characterId, player, reachable) {
  const panel = document.createElement("section"); panel.id = "shared-life-profile";
  const heading = document.createElement("h2"); heading.textContent = "一起度过的时光"; panel.append(heading);
  const statuses = {pending: "等待回应", active: "正在一起进行", completed: "已经完成", declined: "已婉拒", cancelled: "已撤回或过期", interrupted: "已中止"};
  const labels = {meal: "共同用餐", rest: "一起休息", talk: "留时间聊天", care: "陪伴休养", hug: "拥抱", hold_hands: "牵手"};
  for (const moment of (state.living?.shared_moments || []).filter(item => item.other_id === characterId)) {
    const row = document.createElement("div"); row.className = "life-item";
    const text = document.createElement("p"); text.textContent = `${moment.label} · ${statuses[moment.status]} · ${moment.minutes}世界分钟${moment.kind==="meal"?` · ${mealSupplyText(moment,player.id)}`:""}${moment.reason ? `：${moment.reason}` : ""}`;
    if(moment.journey_id)text.textContent+=` · 旅行第${moment.journey_stop_index+1}站的另行相处；结束后不会自动恢复旅行。`;
    row.append(text);
    const add = (name, operation) => {
      const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = name;
      button.addEventListener("click", () => submitSharedLife(characterId, {operation, moment_id: moment.id, statement: operation==="accept"&&moment.kind==="meal"?`我明确接受这次共同用餐，${moment.meal_supply==="proposer"?"由你提供双方各一份食物":"我们各用自己的一份食物"}，持续${moment.minutes}个世界分钟。`:name})); row.append(button);
    };
    if (moment.status === "pending" && moment.recipient_id === player.id) {
      add("我愿意一起度过这段时间", "accept"); add("这次先不了", "decline");
    } else if (moment.status === "pending" && moment.proposer_id === player.id) add("撤回这次邀请", "end");
    else if (moment.status === "active") add("结束这次共同活动", "end");
    panel.append(row);
  }
  const form = document.createElement("form"); form.className = "long-term-form";
  const label = document.createElement("label"); label.textContent = "一起做点什么";
  const kind = document.createElement("select"); kind.setAttribute("aria-label", "一起做点什么");
  for (const [value, text] of Object.entries(labels)) { const option = document.createElement("option"); option.value = value; option.textContent = text; kind.append(option); }
  label.append(kind); form.append(label);
  const supply=appendMealSupplyField(form,kind);
  const durationLabel = document.createElement("label"); durationLabel.textContent = "相处时间（世界分钟，拥抱为1分钟）";
  const minutes = document.createElement("input"); minutes.type = "number"; minutes.min = "1"; minutes.max = "120"; minutes.value = "15"; minutes.required = true;
  durationLabel.append(minutes); form.append(durationLabel);
  const invite = document.createElement("button"); invite.type = "submit"; invite.className = "button primary"; invite.textContent = "向对方提出邀请"; invite.disabled = !reachable;
  form.append(invite); form.addEventListener("submit", event => {
    event.preventDefault(); if(!form.reportValidity())return;submitSharedLife(characterId, {operation: "invite", kind: kind.value, minutes: Number(minutes.value),meal_supply:supply.value,
      statement: `我想邀请你${labels[kind.value]}${kind.value === "hug" ? "一下" : `${minutes.value}分钟`}${kind.value==="meal"?(supply.value==="proposer"?"，我愿意提供双方各一份自己的可用随身食物":"，我们各吃一份自己的可用随身食物"):""}，你愿意吗？`}, true);
  });
  panel.append(form);
  const note = document.createElement("p"); note.textContent = "双方同意后才开始。用餐各自备食，或由邀请者明确提供两份自有食物；接受请客时需要接过一份食物的背包空间。已吃掉的食物不因中止退回，请客不附带欠款或亲密义务。休息按真实时间恢复；牵手和拥抱需走近到3米内，并单独同意。";
  panel.append(note);
  const memoryPanel = document.createElement("section"); panel.append(memoryPanel);
  const renderMemories = memories => {
    memoryPanel.replaceChildren();
    if (memories.length) {
      const heading = document.createElement("h3"); heading.textContent = "你记得的共同经历"; memoryPanel.append(heading);
      for (const memory of memories) { const text = document.createElement("p"); text.textContent = `${formatWorldTime(memory.world_time)} · ${memory.meaning}`; memoryPanel.append(text); }
    }
  };
  renderMemories((state.living?.relationship_memories || []).filter(item => item.other_id === characterId));
  const worldId = state.worldId;
  api(`/api/worlds/${worldId}/relationships/${characterId}/memories`).then(memories => {
    if (panel.isConnected && state.worldId === worldId) renderMemories(memories);
  }).catch(() => {});
  elements.characterProfile.append(panel);
}

const intimacyOriginalKnownProfile = openKnownProfile;
openKnownProfile = function(characterId) {
  intimacyOriginalKnownProfile(characterId);
  appendIntimacyProfile(characterId);
};

const intimacyOriginalLiving = renderLiving;
renderLiving = function() {
  intimacyOriginalLiving();
  let panel = document.querySelector("#intimacy-bonds");
  if (!panel) { panel = document.createElement("section"); panel.id = "intimacy-bonds"; elements.sceneCharacters.after(panel); }
  panel.replaceChildren();
  const bonds = (state.living?.intimate_bonds || []).filter((bond) => ["pending", "active"].includes(bond.status));
  if (bonds.length) {
    const heading = document.createElement("h2"); heading.textContent = "心意与关系"; panel.append(heading);
    for (const bond of bonds) {
      const button = document.createElement("button"); button.type = "button"; button.className = "button secondary";
      const known = state.living?.known_people?.find((person) => person.id === bond.other_id);
      button.textContent = `${known?.name || "熟识的人"} · ${bond.label} · ${intimacyStatus[bond.status]}`;
      button.addEventListener("click", () => openCharacterProfile(bond.other_id)); panel.append(button);
    }
  }
  panel.hidden = !bonds.length;
};
