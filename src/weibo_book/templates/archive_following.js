const followingView=document.querySelector('[data-view="following"]');
const followingTabBar=document.querySelector('[data-tab-bar]');
const followingCategoryDefs=[{key:"blogger",label:"关注博主"},{key:"supertopic",label:"关注超话"},{key:"history",label:"历史变化"}];
let followingCurrentCategory="blogger";
let followingSortMode="real_followed";
function switchTab(name){
document.querySelectorAll('[data-tab]').forEach(btn=>{btn.setAttribute('aria-pressed',String(btn.dataset.tab===name))});
document.querySelectorAll('[data-tab-panel]').forEach(panel=>{panel.hidden=panel.dataset.tabPanel!==name});
const titleEl=document.querySelector('[data-title]');
if(titleEl)titleEl.textContent=name==="following"?"关注资料":"微博书";
if(name==="following")renderFollowingView();
}
function renderFollowingView(){
if(!followingView)return;
const data=archive.following;
const empty=followingView.querySelector('[data-following-empty]');
const layout=followingView.querySelector('.following-layout');
if(!data){
if(empty)empty.hidden=false;
if(layout)layout.hidden=true;
const oldSummary=followingView.querySelector('.following-summary');
if(oldSummary)oldSummary.remove();
return;
}
if(empty)empty.hidden=true;
if(layout)layout.hidden=false;
const oldSummary=followingView.querySelector('.following-summary');
if(oldSummary)oldSummary.remove();
followingView.insertBefore(renderFollowingSummary(data),layout);
const categories=followingView.querySelector('[data-following-categories]');
if(categories){
categories.replaceChildren();
followingCategoryDefs.forEach(def=>{
const count=(data.items||[]).filter(i=>i.object_type===def.key).length;
const btn=el("button","following-category local-control");
btn.type="button";
btn.dataset.category=def.key;
btn.append(el("span","",def.label));
if(def.key!=="history")btn.append(el("span","following-category-count",String(count)));
categories.append(btn);
});
}
selectFollowingCategory("blogger",true);
}
function selectFollowingCategory(key,initial=false){
followingCurrentCategory=key;
document.querySelectorAll('[data-category]').forEach(btn=>{btn.setAttribute('aria-pressed',String(btn.dataset.category===key))});
renderFollowingList(key);
setFollowingLevel(initial?"category":"list");
}
function renderFollowingList(category){
const data=archive.following;
const list=document.querySelector('[data-following-list]');
if(!list)return;
list.replaceChildren();
list.append(followingBackButton());
if(category==="history"){list.append(renderFollowingChangesList(data));return}
const sortBar=el("div","following-sort-bar");
const select=el("select","following-sort-select local-control");
select.dataset.followingSort="";
const options=[{value:"real_followed",label:"真实关注日期：最新优先"},{value:"source_order",label:"微博返回顺序"},{value:"name",label:"名称顺序"},{value:"local_first_seen",label:"本地首次记录"}];
options.forEach(opt=>{
const o=el("option","",opt.label);
o.value=opt.value;
if(opt.value===followingSortMode)o.selected=true;
select.append(o);
});
select.addEventListener("change",()=>{followingSortMode=select.value;renderFollowingList(category);});
sortBar.append(select);
list.append(sortBar);
const unconfirmed=(category==="blogger"&&data.snapshot&&data.snapshot.unconfirmed_bloggers)||[];
if(unconfirmed.length){
list.append(el("div","following-unconfirmed-head",`状态未确认 ${unconfirmed.length} 个（平台未返回，未计入取消关注）`));
unconfirmed.forEach(entry=>{
const row=el("div","following-item following-item-unconfirmed");
row.append(el("span","following-item-name",entry.name));
row.append(el("span","following-item-meta","状态未确认"));
list.append(row);
});
}
const items=sortFollowingItems((data.items||[]).filter(i=>i.object_type===category),data);
items.forEach(item=>{
const row=el("button","following-item local-control");
row.type="button";
row.dataset.objectType=item.object_type;
row.dataset.objectId=item.object_id;
row.append(el("span","following-item-name",item.display_name));
if(item.platform_followed_at)row.append(el("span","following-item-meta",item.platform_followed_at));
else row.append(el("span","following-item-meta","真实日期未知"));
list.append(row);
});
if(!items.length)list.append(el("div","following-empty","该分类暂无数据"));
}
function followingBackButton(){
const btn=el("button","following-back local-control","‹ 返回");
btn.type="button";
btn.dataset.action="following-back";
return btn;
}
function selectFollowingObject(type,id){
const data=archive.following;
const item=(data.items||[]).find(i=>i.object_type===type&&i.object_id===id);
if(!item)return;
const detail=document.querySelector('[data-following-detail]');
if(detail){detail.replaceChildren();detail.append(followingBackButton(),renderFollowingDetail(item))}
setFollowingLevel("detail");
}
function renderFollowingDetail(item){
const data=archive.following;
const rel=(data.relationships||[]).find(r=>r.object_type===item.object_type&&r.object_id===item.object_id);
const names=(data.names||[]).filter(n=>n.object_type===item.object_type&&n.object_id===item.object_id);
const wrap=el("div");
wrap.append(detailRow("当前名称",item.display_name));
wrap.append(detailRow("数字身份",item.object_id));
if(item.page_url)wrap.append(detailRow("页面入口",item.page_url));
wrap.append(detailRow("关注状态",rel?rel.active?"仍在关注":"已取消关注":"未记录"));
if(rel){
const duration=followingDurationText(data.snapshot.cutoff_at,rel);
wrap.append(detailRow("关注时长",duration.text));
wrap.append(detailRow("时长来源",duration.source));
}
wrap.append(detailRow("截止时间",data.snapshot.cutoff_at||"未知"));
if(rel){
wrap.append(detailRow("本地首次发现",rel.local_first_seen_at||"未知"));
wrap.append(detailRow("最后确认",rel.last_confirmed_at||"未知"));
}
wrap.append(renderNameRecords(names));
wrap.append(renderIdentityActions(item));
return wrap;
}
function sortFollowingItems(items,data){
const rels=data.relationships||[];
const relMap=new Map();
rels.forEach(r=>relMap.set(r.object_type+":"+r.object_id,r));
const withRel=items.map(item=>({item,rel:relMap.get(item.object_type+":"+item.object_id)}));
if(followingSortMode==="source_order"){
withRel.sort((a,b)=>a.item.source_order-b.item.source_order);
}else if(followingSortMode==="name"){
withRel.sort((a,b)=>a.item.display_name.localeCompare(b.item.display_name));
}else if(followingSortMode==="local_first_seen"){
withRel.sort((a,b)=>{
const aTime=a.rel?new Date(a.rel.local_first_seen_at).getTime():0;
const bTime=b.rel?new Date(b.rel.local_first_seen_at).getTime():0;
return (bTime||0)-(aTime||0);
});
}else{
withRel.sort((a,b)=>{
const aPlat=a.rel&&a.rel.platform_followed_at?new Date(a.rel.platform_followed_at).getTime():null;
const bPlat=b.rel&&b.rel.platform_followed_at?new Date(b.rel.platform_followed_at).getTime():null;
if(aPlat!==null&&bPlat!==null)return bPlat-aPlat;
if(aPlat!==null)return -1;
if(bPlat!==null)return 1;
return a.item.source_order-b.item.source_order;
});
}
return withRel.map(x=>x.item);
}
function renderIdentityActions(item){
const wrap=el("div","following-identity-actions");
if(item.page_url){
const openBtn=el("a","following-action following-action-primary","打开主页");
openBtn.href=item.page_url;
openBtn.target="_blank";
openBtn.rel="noopener noreferrer";
wrap.append(openBtn);
const copyLinkBtn=el("button","following-action local-control","复制主页链接");
copyLinkBtn.type="button";
copyLinkBtn.dataset.copyText=item.page_url;
wrap.append(copyLinkBtn);
}
if(item.app_scheme){
const openAppBtn=el("a","following-action","打开应用");
openAppBtn.href=item.app_scheme;
wrap.append(openAppBtn);
}
const copyIdBtn=el("button","following-action local-control","复制数字身份");
copyIdBtn.type="button";
copyIdBtn.dataset.copyText=item.object_id;
wrap.append(copyIdBtn);
const nameBtn=el("button","following-action local-control","查看名称记录");
nameBtn.type="button";
nameBtn.dataset.action="following-name-records";
wrap.append(nameBtn);
return wrap;
}
function followingCopyText(text){
if(navigator.clipboard&&navigator.clipboard.writeText){
navigator.clipboard.writeText(text).catch(()=>followingCopyFallback(text));
}else{
followingCopyFallback(text);
}
}
function followingCopyFallback(text){
const ta=document.createElement("textarea");
ta.value=text;
ta.style.position="fixed";
ta.style.opacity="0";
document.body.append(ta);
ta.select();
try{document.execCommand("copy")}catch(_e){}
ta.remove();
}
function followingDurationText(cutoff,rel){
if(rel.platform_followed_at){
const days=followingDaysBetween(rel.platform_followed_at,cutoff);
return{text:days!==null?`已关注 ${days} 天`:"未知",source:"微博原始值"};
}
const days=followingDaysBetween(rel.local_first_seen_at,cutoff);
return{text:days!==null?`自本地首次记录起至少 ${days} 天`:"未知",source:"本地最短记录"};
}
function followingDaysBetween(start,end){
if(!start||!end)return null;
const s=new Date(start),e=new Date(end);
if(isNaN(s.getTime())||isNaN(e.getTime()))return null;
const days=Math.floor((e.getTime()-s.getTime())/86400000);
return days>=0?days:null;
}
function renderNameRecords(names){
const wrap=el("div");
wrap.append(el("div","following-detail-title","名称记录"));
if(!names.length){wrap.append(el("div","following-detail-value","无名称记录"));return wrap}
const list=el("div","following-name-records");
names.forEach(n=>{
const row=el("div","following-name-record"+(n.current?" current":""),`${n.name}（${n.first_seen_at||"未知"}起）`);
list.append(row);
});
wrap.append(list);
return wrap;
}
function detailRow(label,value){
const row=el("div","following-detail-row");
row.append(el("span","following-detail-label",label),el("span","following-detail-value",String(value)));
return row;
}
function renderFollowingChangesList(data){
const wrap=el("div","following-changes-list");
const changes=data.changes||[];
if(!changes.length){wrap.append(el("div","following-empty","首次建立关注资料"));return wrap}
Array.from(changes).reverse().forEach(group=>{
const event=el("div","following-change-event");
const parts=[];
if(group.followed)parts.push(`新增关注 ${group.followed}`);
if(group.unfollowed)parts.push(`取消关注 ${group.unfollowed}`);
if(group.renamed)parts.push(`名称变化 ${group.renamed}`);
if(group.refollowed)parts.push(`重新关注 ${group.refollowed}`);
event.append(el("b","",`完整快照 ${group.snapshot_id}`),el("div","",parts.join(" · ")||"无变化"));
wrap.append(event);
});
return wrap;
}
function setFollowingLevel(level){
const layout=document.querySelector('.following-layout');
if(layout)layout.dataset.followingLevel=level;
}
function renderFollowingSummary(data){
const wrap=el("div","following-summary");
const head=el("div","following-summary-head");
head.append(el("strong","","关注资料快照"),el("span","following-summary-status",data.snapshot.status==="complete"?"完整成功":data.snapshot.status));
wrap.append(head);
const cutoff=data.snapshot.cutoff_at||"未知";
wrap.append(el("div","following-summary-cutoff",`截止时间：${cutoff}`));
const metrics=el("div","following-summary-metrics");
const realDurationCount=Array.from(data.relationships||[]).filter(rel=>rel.active&&rel.platform_followed_at).length;
const unconfirmed=(data.snapshot&&data.snapshot.unconfirmed_bloggers)||[];
metrics.append(followingMetric(data.snapshot.blogger_count||0,"关注博主"),followingMetric(data.snapshot.supertopic_count||0,"关注超话"),followingMetric(realDurationCount,"真实时长"));
if(unconfirmed.length)metrics.append(followingMetric(unconfirmed.length,"状态未确认"));
wrap.append(metrics);
if(unconfirmed.length)wrap.append(el("div","following-unconfirmed-note",`平台未返回 ${unconfirmed.length} 个关注博主，可能已被平台隐藏或已取消关注，未计入取消关注`));
const changes=data.changes||[];
const changeSummary=el("div","following-change-summary");
changeSummary.append(el("div","following-detail-title","最新变化"));
if(changes.length){
const latest=Array.from(changes).reverse()[0];
const parts=[];
if(latest.followed)parts.push("新增关注 "+latest.followed);
if(latest.unfollowed)parts.push("取消关注 "+latest.unfollowed);
if(latest.renamed)parts.push("名称变化 "+latest.renamed);
if(latest.refollowed)parts.push("重新关注 "+latest.refollowed);
changeSummary.append(el("div","",parts.join(" · ")||"无变化"));
}else{
changeSummary.append(el("div","","首次建立关注资料"));
}
wrap.append(changeSummary);
return wrap;
}
function followingMetric(value,label){
const m=el("div","following-metric");
m.append(el("b","",String(value)),el("span","",label));
return m;
}
if(followingTabBar){followingTabBar.addEventListener("click",event=>{const btn=event.target.closest('[data-tab]');if(btn)switchTab(btn.dataset.tab)})}
if(followingView){
followingView.addEventListener("click",event=>{
const catBtn=event.target.closest('[data-category]');
if(catBtn){selectFollowingCategory(catBtn.dataset.category);return}
const itemBtn=event.target.closest('[data-object-id]');
if(itemBtn){selectFollowingObject(itemBtn.dataset.objectType,itemBtn.dataset.objectId);return}
if(event.target.closest('[data-action="following-back"]')){
const layout=document.querySelector('.following-layout');
const level=layout?layout.dataset.followingLevel:"category";
if(level==="detail")setFollowingLevel("list");
else if(level==="list")setFollowingLevel("category");
return;
}
const copyBtn=event.target.closest('[data-copy-text]');
if(copyBtn){followingCopyText(copyBtn.dataset.copyText);return}
if(event.target.closest('[data-action="following-name-records"]')){
const detail=document.querySelector('[data-following-detail]');
const nameRecords=detail&&detail.querySelector('.following-name-records');
if(nameRecords)nameRecords.scrollIntoView({behavior:"smooth",block:"center"});
return;
}
});
}
