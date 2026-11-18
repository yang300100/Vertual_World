/* 哥哥，家庭承诺、住房许可和本人资金分别交给世界结算。 */
Object.assign(eventLabels, {"social.family_proposed":"家庭提议","social.family_accepted":"家庭承诺确认",
  "social.family_ended":"家庭约定结束","social.family_declined":"家庭提议被拒绝","social.family_contribute":"投入家庭资金",
  "social.family_withdraw":"取回本人贡献","social.family_child_born":"家庭迎来孩子","social.family_child_fed":"照料孩子",
  "social.family_housing_suspended":"共同住所暂停","social.player_age_declared":"人物年龄确认"});
const familyLabels={marriage:"婚姻",cohabit:"共同居住",family:"共同家庭",parenthood:"共同养育计划",allowance:"家庭资金拨付"};
const familyStatuses={pending:"等待回应",active:"双方已确认",declined:"已拒绝",ended:"已结束",suspended:"安排暂停",completed:"已完成",expired:"提议已过期"};

async function submitFamilyChoice(characterId,choice,talk=false) {
  let applied=false;
  elements.characterDialog.close();
  await perform("正在核对家庭安排",async()=>{
    const result=await api(`/api/worlds/${state.worldId}/families`,{method:"POST",body:JSON.stringify({recipient_id:characterId,choice})});
    applied=true;showActionResult(result.summary);return result;
  },"家庭选择已记录");
  if(applied&&talk) {
    const person=characterById(characterId),player=currentPlayer();
    const near=person&&canInviteCompanion(person)&&distanceKm(player.longitude,player.latitude,person.longitude,person.latitude)<=0.02;
    if(near) executePlayerIntent(`说话：${choice.statement}`,characterId,true);
    else if(state.contacts.some(contact=>contact.recipient_id===characterId)) {
      await perform("正在通过信笺商量家庭安排",async()=>{
        const response=await api(`/api/worlds/${state.worldId}/messages`,{method:"POST",body:JSON.stringify({recipient_id:characterId,content:choice.statement})});
        showActionResult(response.reply);return response;
      },"信笺已得到回应");
    }
  }
}

function familyButton(label,action) {
  const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent=label;button.addEventListener("click",action);return button;
}

function appendLifeDetails(character) {
  if(document.querySelector("#player-life-details"))return;
  const panel=document.createElement("section");panel.id="player-life-details";
  const heading=document.createElement("h2");heading.textContent="我的人物年龄";panel.append(heading);
  const note=document.createElement("p");note.textContent=character.age_years==null?"人物年龄尚未记录。请按你的世界人物设定填写；婚姻等长期承诺需要已知的成年资料。":"已确认的人物年龄随世界时间增长，不在此改写既有出生时间。";panel.append(note);
  const form=document.createElement("form");form.className="long-term-form";
  const label=document.createElement("label");label.textContent="世界人物年龄（岁）";
  const input=document.createElement("input");input.type="number";input.min="0";input.max="10000";input.required=true;input.value=character.age_years??"";input.disabled=character.age_years!=null;
  label.append(input);form.append(label);
  if(character.age_years==null) {
    const submit=document.createElement("button");submit.className="button primary";submit.type="submit";submit.textContent="确认我的人物年龄";form.append(submit);
    form.addEventListener("submit",event=>{
      event.preventDefault();elements.characterDialog.close();
      perform("正在记录你的人物年龄",()=>api(`/api/worlds/${state.worldId}/player/life-details`,{method:"POST",body:JSON.stringify({age_years:Number(input.value)})}),"人物年龄已记录");
    });
  }
  panel.append(form);elements.characterProfile.append(panel);
}

function appendFamilyProfile(characterId) {
  if(document.querySelector("#family-profile"))return;
  const player=currentPlayer(),character=characterById(characterId);
  if(!player||!character)return;
  if(character.is_player){appendLifeDetails(character);return;}
  const panel=document.createElement("section");panel.id="family-profile";
  const heading=document.createElement("h2");heading.textContent="家庭与共同住所";panel.append(heading);
  const note=document.createElement("p");note.textContent="婚姻、同居和组建家庭分别表达意愿。共同住所需要真实许可；个人财物、身体界限和原有租约继续有效。";panel.append(note);
  if(player.age_years==null)panel.append(familyButton("先填写我的人物年龄",()=>{elements.characterDialog.close();openCharacterProfile(player.id);}));
  const agreements=(state.living?.family_agreements||[]).filter(a=>a.other_id===characterId);
  for(const agreement of agreements) {
    const row=document.createElement("div");row.className="life-item";
    const text=document.createElement("p");text.textContent=`${agreement.title} · ${familyStatuses[agreement.status]}${agreement.room_name?` · 住所：${agreement.room_name}`:""}：${agreement.statement}${agreement.response?` / ${agreement.response}`:""}`;row.append(text);
    if(agreement.kind==="marriage"){const terms=document.createElement("p");terms.textContent=agreement.exclusive?"本次提议包含彼此排他的婚姻约定。":"本次提议不包含彼此排他的婚姻约定。";row.append(terms);}
    const apply=(label,operation)=>row.append(familyButton(label,()=>submitFamilyChoice(characterId,{operation,agreement_id:agreement.id,statement:label})));
    if(agreement.status==="pending") {
      if(agreement.recipient_id===player.id){apply("我同意这份家庭安排","accept");apply("我暂时不同意","decline");}
      else apply("撤回这份提议","end");
    } else if(["active","suspended"].includes(agreement.status))apply("结束这份约定","end");
    if(agreement.kind==="parenthood"&&agreement.child_plan) {
      const plan=document.createElement("p");const labels={planned:"计划已获准，等待世界时间",due:"已到计划日期",completed:"已登记出生",cancelled:"已取消"};
      plan.textContent=agreement.child_plan.status==="completed"?`${agreement.child_name}已按计划完成出生登记。`:`${agreement.child_name}的养育计划：${labels[agreement.child_plan.status]||agreement.child_plan.status}，可登记时间 ${formatWorldTime(agreement.child_plan.due_at_world)}。到期后双方须实际回到共同住所，再登记出生。`;row.append(plan);
      if(agreement.status==="active"){
        const birth=familyButton("按到期计划登记出生",()=>submitFamilyChoice(characterId,{operation:"register_birth",agreement_id:agreement.id,statement:"我们申请按到期计划登记出生。"}));
        const now=Date.parse(state.snapshot?.world?.current_time||"");birth.disabled=!Number.isFinite(now)||Date.parse(agreement.child_plan.due_at_world)>now;
        row.append(birth);
      }
    }
    if(agreement.kind==="family"&&agreement.status==="active") {
      const own=(agreement.accounts||[]).find(a=>a.contributor_id===player.id)?.balance||0;
      const amountLabel=document.createElement("label");amountLabel.textContent=`家庭余额 ${agreement.balance} 枚；本人未使用贡献 ${own} 枚。金额 `;
      const amount=document.createElement("input");amount.type="number";amount.min="1";amount.max="1000000";amount.value="1";amount.setAttribute("aria-label","家庭资金金额");amountLabel.append(amount);row.append(amountLabel);
      for(const [op,label] of [["contribute","投入我的家庭资金"],["withdraw","取回本人未使用贡献"]])row.append(familyButton(label,()=>submitFamilyChoice(characterId,{operation:op,agreement_id:agreement.id,amount:Number(amount.value),statement:label})));
    }
    panel.append(row);
  }
  const form=document.createElement("form");form.className="long-term-form";
  const addField=(text,node)=>{const label=document.createElement("label");label.textContent=text;label.append(node);form.append(label);return label;};
  const kind=document.createElement("select");kind.setAttribute("aria-label","家庭安排类型");
  for(const [value,label] of Object.entries(familyLabels)){const option=document.createElement("option");option.value=value;option.textContent=label;kind.append(option);}addField("想商量的家庭安排",kind);
  const title=document.createElement("input");title.maxLength=80;title.placeholder="例如：河边之家，或购粮用途";addField("家庭名称或资金用途",title);
  const home=document.createElement("select");home.setAttribute("aria-label","共同住所");const blank=new Option("请选择已有许可的住所","");home.append(blank);const homeLabel=addField("共同住所",home);
  const amount=document.createElement("input");amount.type="number";amount.min="1";amount.max="1000000";amount.value="1";const amountLabel=addField("申请拨给本人的金额",amount);
  const child=document.createElement("input");child.maxLength=60;const childLabel=addField("双方商量的孩子姓名",child);
  const species=document.createElement("select");const speciesLabel=addField("已登记的孩子种族",species);
  const exclusive=document.createElement("input");exclusive.type="checkbox";exclusive.checked=true;const exclusiveLabel=addField("婚姻约定彼此排他",exclusive);
  const statement=document.createElement("textarea");statement.required=true;statement.maxLength=240;statement.setAttribute("aria-label","家庭提议原话");statement.placeholder="坦白表达希望，也给对方独立决定的空间……";addField("想对对方说的话",statement);
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出家庭安排并听听回应";form.append(submit);
  const update=()=>{
    homeLabel.hidden=kind.value!=="cohabit";home.required=kind.value==="cohabit";
    amountLabel.hidden=kind.value!=="allowance";amount.required=kind.value==="allowance";
    childLabel.hidden=speciesLabel.hidden=kind.value!=="parenthood";child.required=species.required=kind.value==="parenthood";
    exclusiveLabel.hidden=kind.value!=="marriage";
  };
  kind.addEventListener("change",update);update();
  form.addEventListener("submit",event=>{
    event.preventDefault();if(!statement.value.trim())return;
    const choice={operation:"propose",kind:kind.value,title:title.value.trim(),statement:statement.value.trim(),exclusive:exclusive.checked};
    if(kind.value==="cohabit")choice.room_id=home.value;
    if(kind.value==="allowance")choice.amount=Number(amount.value);
    if(kind.value==="parenthood"){choice.child_name=child.value.trim();choice.child_species=species.value;}
    submitFamilyChoice(characterId,choice,true);
  });
  panel.append(form);
  const authorization=document.createElement("details");authorization.className="long-term-form";
  const summary=document.createElement("summary");summary.textContent="居住许可（住所所有者）";authorization.append(summary);
  const permitHome=document.createElement("select");permitHome.setAttribute("aria-label","本人拥有的住所");permitHome.append(new Option("选择本人拥有的住所",""));authorization.append(permitHome);
  const permit=(operation,statement)=>{
    if(!permitHome.value)return showToast("先选择本人拥有的住所",true);
    submitFamilyChoice(characterId,{operation,room_id:permitHome.value,statement});
  };
  authorization.append(familyButton("允许对方在所选的本人住所居住",()=>permit("authorize_residence","我同意你在我拥有的这间住所居住。")));
  authorization.append(familyButton("撤回对方在所选本人住所的居住许可",()=>permit("revoke_residence","我决定撤回这份居住许可。")));
  panel.append(authorization);
  const familyIds=agreements.filter(a=>a.kind==="family").map(a=>a.id);
  for(const child of (state.living?.family_children||[]).filter(child=>familyIds.includes(child.family_id))) {
    const text=document.createElement("p");text.textContent=`家庭成员：${child.name}，出生于 ${formatWorldTime(child.birth_world_time)}`;panel.append(text);
    panel.append(familyButton(`用我的一份食物照料${child.name}`,()=>submitFamilyChoice(characterId,{operation:"feed_child",child_id:child.id,statement:"我想照料孩子。"})));
  }
  elements.characterProfile.append(panel);
  const world=state.worldId;
  api(`/api/worlds/${world}/families/options/${characterId}`).then(options=>{
    if(!panel.isConnected||state.worldId!==world)return;
    for(const room of options.home_options||[]){home.append(new Option(room.name,room.id));if(room.owned)permitHome.append(new Option(room.name,room.id));}
    for(const item of options.species_options||[])species.append(new Option(item.display_name,item.id));
  }).catch(()=>{});
}

const familyOriginalProfile=openCharacterProfile;
openCharacterProfile=async function(characterId){await familyOriginalProfile(characterId);appendFamilyProfile(characterId);};
const familyOriginalKnownProfile=openKnownProfile;
openKnownProfile=function(characterId){familyOriginalKnownProfile(characterId);appendFamilyProfile(characterId);};
