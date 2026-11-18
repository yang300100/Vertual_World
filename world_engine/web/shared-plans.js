/* 哥哥，分工只承诺各自份额，核对完成不替双方消费、赠与或出发。 */
const sharedPlanStatuses={pending:"等待双方确认分工",active:"各自筹备中",completed:"双方已共同核对筹备",declined:"已婉拒",ended:"已结束",expired:"期限已到"};
for(const [status,label] of Object.entries(sharedPlanStatuses))eventLabels[`social.appointment_preparation_${status}`]=label;
Object.assign(eventLabels,{"social.appointment_preparation_proposed":"提出共同筹备","social.appointment_preparation_checked_in":"本人核对筹备"});
const preparationTaskNames={earn_money:"通过劳动筹钱",prepare_food:"备齐本人食物"};

function submitSharedPlanChoice(characterId,choice) {
  const person=characterById(characterId),player=currentPlayer();
  const near=person&&player&&canInviteCompanion(person)&&distanceKm(player.longitude,player.latitude,person.longitude,person.latitude)<=0.02;
  elements.characterDialog.close();
  return executeNaturalSocialChoice(choice.statement,{target_character_id:characterId,choice_evidence:choice.statement,shared_plan_choice:choice},near?null:"letter");
}

function submitPreparationActivity(characterId,choice) {
  const person=characterById(characterId),player=currentPlayer();
  const near=person&&player&&canInviteCompanion(person)&&distanceKm(player.longitude,player.latitude,person.longitude,person.latitude)<=0.02;
  elements.characterDialog.close();
  return executeNaturalSocialChoice(choice.statement,{target_character_id:characterId,choice_evidence:choice.statement,shared_life_choice:choice},near?null:"letter");
}

async function appendSharedPlansProfile(characterId) {
  const player=currentPlayer(),person=characterById(characterId);
  if(!player||!person||person.is_player||document.querySelector("#shared-plans-profile"))return;
  const panel=document.createElement("section");panel.id="shared-plans-profile";
  const heading=document.createElement("h2");heading.textContent="一起筹备与分工";panel.append(heading);
  const note=document.createElement("p");note.textContent="商量各自负责筹钱或备食，到约定地点一起核对。资源仍各自保管，后续使用或出行由双方再商量。";panel.append(note);
  elements.characterProfile.append(panel);
  let plans,options;
  try { [plans,options]=await Promise.all([api(`/api/worlds/${state.worldId}/shared-plans?character_id=${encodeURIComponent(characterId)}`),api(`/api/worlds/${state.worldId}/appointments/options/${characterId}`)]); }
  catch {const error=document.createElement("p");error.textContent="暂时无法读取筹备记录，请稍后重新打开档案。";panel.append(error);return;}
  if(!panel.isConnected)return;
  for(const plan of plans.filter(p=>p.other_id===characterId)) {
    const row=document.createElement("div");row.className="life-item";
    const text=document.createElement("p");text.textContent=`${plan.title} · ${sharedPlanStatuses[plan.status]} · ${plan.location_name}${plan.room_name?` / ${plan.room_name}`:" / 室外"} · 期限 ${formatWorldTime(plan.deadline_at)}`;row.append(text);
    const statement=document.createElement("p");statement.textContent=plan.statement+(plan.response?` / ${plan.response}`:"");row.append(statement);
    for(const role of plan.roles) {
      const line=document.createElement("p"),own=role.character_id===player.id,unit=role.kind==="earn_money"?"枚":"份";
      line.textContent=`${own?"你负责":"对方负责"}${role.label}${role.target}${unit}${role.kind==="prepare_food"?`，采购总上限${role.spending_limit}枚`:""}。`+
        (own?(plan.status==="pending"?"等待双方确认后再核对。":`${plan.status==="completed"?"共同核对时满足":"最近记录"}${role.progress}/${role.target}${unit}${role.spent?`，已为筹备采购支出${role.spent}枚`:""}。`):role.ready_at?"对方曾明确核对就绪。":"等待对方本人说明筹备进度。")+
        (role.ready_at?` 上次核对 ${formatWorldTime(role.ready_at)}${role.readiness_fresh||plan.status!=="active"?"":"，需要本人重新核对"}。`:"")+(own&&role.reason?` ${role.reason}`:"");row.append(line);
    }
    if(plan.reason){const reason=document.createElement("p");reason.textContent=plan.reason;row.append(reason);}
    const add=(label,operation,statement)=>{const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent=label;
      button.addEventListener("click",()=>submitSharedPlanChoice(characterId,{operation,plan_id:plan.id,statement}));row.append(button);};
    if(plan.status==="pending") {
      if(plan.proposer_id!==player.id){const own=plan.roles.find(r=>r.character_id===player.id);
        add("接受自己这份分工","accept",`我同意自己负责${own.label}${own.target}${own.kind==="earn_money"?"枚":"份"}，${own.kind==="prepare_food"?`为这次筹备采购最多${own.spending_limit}枚，`:""}到约定地点与对方核对，资源仍各自保管。`);
        add("婉拒这份筹备分工","decline","谢谢你愿意商量，但这份分工我暂时不接受。");
      } else add("撤回我的筹备提议","end","我先撤回这份共同筹备提议。");
    }
    if(plan.status==="active") {
      add("到场核对我的筹备","check_in","我已到约定地点，愿意核对自己负责的筹备份额。");
      if(plan.roles.some(r=>r.character_id===player.id&&r.ready_at))add("撤回我上次的就绪表示","withdraw_readiness","我想先撤回上次的就绪表示，还需要继续准备。");
      add("结束这份共同筹备","end","我决定结束这份共同筹备，已经发生的劳动与支出继续保留，各自保管财物。");
    }
    for(const moment of plan.follow_through||[]) {
      const labels={pending:"等待回应",active:"正在相处",completed:"已完成",declined:"已婉拒",cancelled:"已撤回或过期",interrupted:"已中止"};
      const kinds={meal:"共同用餐",rest:"一起休息",talk:"留时间聊天"};
      const detail=document.createElement("p");detail.textContent=`筹备后的${kinds[moment.kind]} · ${labels[moment.status]} · ${moment.minutes}个世界分钟${moment.kind==="meal"?` · ${mealSupplyText(moment,player.id)}`:""}${moment.reason?` · ${moment.reason}`:""}`;row.append(detail);
      const act=(label,operation,statement)=>{const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent=label;
        button.addEventListener("click",()=>submitPreparationActivity(characterId,{operation,moment_id:moment.id,statement}));row.append(button);};
      if(moment.status==="pending") {
        if(moment.recipient_id===player.id) {
          act("同意并开始这次相处","accept",`我愿意现在开始这次筹备后的${kinds[moment.kind]}，持续${moment.minutes}个世界分钟${moment.kind==="meal"?(moment.meal_supply==="proposer"?"，由你提供双方各一份食物":"，各吃一份自己的可用随身食物"):""}。`);
          act("婉拒这次相处","decline","谢谢你的邀请，这次筹备后的相处我暂时不参加。");
        } else act("撤回后续相处邀请","end","我先撤回这次筹备后的相处邀请。");
      }
      if(moment.status==="active")act("结束这次相处","end","我想结束这次相处，已经实际使用的食物继续保留消耗记录。");
    }
    if(plan.can_follow_through) {
      const follow=document.createElement("form");follow.className="long-term-form";
      const explanation=document.createElement("p");explanation.textContent="筹备已核对，可以另行邀请相处。用餐按新邀请商量各自备食或由邀请者提供两份食物，预算仍各自保管；另一方同意后才开始。";follow.append(explanation);
      const typeLabel=document.createElement("label");typeLabel.textContent="筹备后的相处";
      const kind=document.createElement("select");kind.setAttribute("aria-label","筹备后的相处");
      for(const [value,label] of [["meal","共同用餐"],["rest","一起休息"],["talk","留时间聊天"]]){const option=document.createElement("option");option.value=value;option.textContent=label;kind.append(option);}typeLabel.append(kind);follow.append(typeLabel);
      const supply=appendMealSupplyField(follow,kind,"筹备后用餐的供餐安排");
      const durationLabel=document.createElement("label");durationLabel.textContent="相处时长（世界分钟）";
      const minutes=document.createElement("input");minutes.type="number";minutes.min="1";minutes.max="120";minutes.value="15";minutes.required=true;minutes.setAttribute("aria-label","相处时长（世界分钟）");durationLabel.append(minutes);follow.append(durationLabel);
      const button=document.createElement("button");button.type="submit";button.className="button secondary";button.textContent="邀请筹备后的相处";follow.append(button);
      follow.addEventListener("submit",event=>{event.preventDefault();if(!follow.reportValidity())return;const label=kind.selectedOptions[0].textContent;
        submitPreparationActivity(characterId,{operation:"invite",kind:kind.value,minutes:Number(minutes.value),preparation_plan_id:plan.id,meal_supply:supply.value,
          statement:`这份共同筹备已经核对完成，我想邀请你现在${label}${minutes.value}个世界分钟${kind.value==="meal"?(supply.value==="proposer"?"，我愿意提供双方各一份自己的可用随身食物":"，各吃一份自己当前可用的随身食物"):""}，预算仍各自保管，你愿意吗？`});});
      row.append(follow);
    }
    panel.append(row);
  }
  const form=document.createElement("form");form.className="long-term-form";
  const field=(name,el)=>{const label=document.createElement("label");label.textContent=name;el.setAttribute("aria-label",name);label.append(el);form.append(label);return el;};
  const option=(select,value,text)=>{const el=document.createElement("option");el.value=value;el.textContent=text;select.append(el);};
  const title=field("筹备名称",document.createElement("input"));title.maxLength=80;title.value="为一次共同出行做准备";title.required=true;
  const place=field("筹备核对地点",document.createElement("select"));for(const p of options.place_options)option(place,p.id,p.name);
  const room=field("筹备核对场景",document.createElement("select"));option(room,"","该地点室外");
  for(const r of options.room_options||[]){option(room,r.id,`${r.location_name} / ${r.name}`);if(!Array.from(place.options).some(o=>o.value===r.location_id))option(place,r.location_id,r.location_name);}
  room.addEventListener("change",()=>{const r=(options.room_options||[]).find(r=>r.id===room.value);if(r)place.value=r.location_id;});place.addEventListener("change",()=>{room.value="";});
  const tasks=[];
  for(const [label,kindValue,targetValue] of [["我的分工","prepare_food",2],["希望对方考虑的分工","earn_money",12]]) {
    const kind=field(label,document.createElement("select"));for(const [value,name] of Object.entries(preparationTaskNames))option(kind,value,name);kind.value=kindValue;
    const target=field(`${label}目标数量`,document.createElement("input"));target.type="number";target.min="1";target.max=kindValue==="prepare_food"?"12":"500";target.value=String(targetValue);target.required=true;
    const budget=field(`${label}采购总上限（枚）`,document.createElement("input"));budget.type="number";budget.min="0";budget.max="100";budget.value="0";budget.required=true;budget.disabled=kindValue==="earn_money";
    kind.addEventListener("change",()=>{target.max=kind.value==="prepare_food"?"12":"500";target.value=kind.value==="prepare_food"?"2":"12";budget.value="0";budget.disabled=kind.value==="earn_money";});
    tasks.push(()=>({kind:kind.value,target:Number(target.value),spending_limit:kind.value==="prepare_food"?Number(budget.value):0}));
  }
  const deadline=field("筹备期限（世界时间 UTC）",document.createElement("input"));deadline.type="datetime-local";deadline.required=true;
  const at=Date.parse(state.living?.world_time||"");if(Number.isFinite(at))deadline.value=new Date(at+3*86400000).toISOString().slice(0,16);
  const speech=field("想如何商量这份分工",document.createElement("textarea"));speech.maxLength=240;speech.required=true;speech.placeholder="说说想一起准备什么，也给对方拒绝或提出顾虑的空间。";
  const details=document.createElement("p");details.textContent="筹钱需要确认后完成真实劳动，并仍持有约定金额；备食需要本人的可用随身食物。采购上限为0时，不授权为该计划采购。";form.append(details);
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出筹备分工，听听对方的决定";form.append(submit);
  const near=canInviteCompanion(person)&&distanceKm(player.longitude,player.latitude,person.longitude,person.latitude)<=0.02;
  submit.disabled=(!near&&!state.contacts.some(c=>c.recipient_id===characterId))||!place.options.length;
  form.addEventListener("submit",event=>{event.preventDefault();if(!form.reportValidity())return;const end=new Date(deadline.value+":00Z");if(!Number.isFinite(end.getTime()))return;
    submitSharedPlanChoice(characterId,{operation:"propose",title:title.value.trim(),location_id:place.value,room_id:room.value||null,deadline_world_time:end.toISOString(),own_task:tasks[0](),other_task:tasks[1](),statement:speech.value.trim()});});
  panel.append(form);
}

const sharedPlansOriginalProfile=openCharacterProfile;
openCharacterProfile=async function(characterId){await sharedPlansOriginalProfile(characterId);await appendSharedPlansProfile(characterId);};
const sharedPlansOriginalKnownProfile=openKnownProfile;
openKnownProfile=function(characterId){sharedPlansOriginalKnownProfile(characterId);appendSharedPlansProfile(characterId);};
