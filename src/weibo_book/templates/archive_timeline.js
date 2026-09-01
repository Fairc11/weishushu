const TIMELINE_WINDOW_SIZE=60;
const TIMELINE_WINDOW_SHIFT=40;
const TIMELINE_MONTH_CONTEXT=20;
const pinnedPosts=archive.posts.filter(post=>post.is_pinned===true);
const normalPosts=archive.posts.filter(post=>post.is_pinned!==true);
const timelineMonths=Array.isArray(archive.timeline&&archive.timeline.months)?archive.timeline.months:[];
const newerWindow=document.querySelector('[data-action="newer-window"]');
const olderWindow=document.querySelector('[data-action="older-window"]');
const latestWindow=document.querySelector('[data-action="latest-window"]');

function clampTimelineStart(value){
  const maximum=Math.max(0,normalPosts.length-TIMELINE_WINDOW_SIZE);
  return Math.min(maximum,Math.max(0,value));
}

function renderTimelineWindow(start,{latest=false,targetKey=null,focusIndex=null}={}){
  const boundedStart=clampTimelineStart(start);
  const end=Math.min(normalPosts.length,boundedStart+TIMELINE_WINDOW_SIZE);
  const fragment=document.createDocumentFragment();
  if(latest)pinnedPosts.forEach(post=>fragment.append(card(post)));
  normalPosts.slice(boundedStart,end).forEach((post,index)=>{
    const article=card(post);
    if(targetKey!==null&&boundedStart+index===focusIndex){
      article.dataset.timelineTarget=targetKey;
    }
    fragment.append(article);
  });
  feed.querySelectorAll("[data-live-photo] video").forEach(video=>{
    video.pause();
  });
  feed.replaceChildren(fragment);
  state.timelineStart=boundedStart;
  state.timelineEnd=end;
  state.timelineLatest=latest;
  state.timelineTarget=targetKey;
  newerWindow.hidden=latest||boundedStart===0;
  latestWindow.hidden=latest;
  olderWindow.hidden=end>=normalPosts.length;
  observeFeedCards();
}

function moveTimelineWindow(direction){
  const next=clampTimelineStart(
    state.timelineStart+(direction*TIMELINE_WINDOW_SHIFT)
  );
  renderTimelineWindow(next,{latest:next===0&&direction<0});
  const selectedKey=monthKeyForIndex(next);
  if(selectedKey)setTimelineSelection(selectedKey);
  timelineProgrammaticScroll=true;
  window.scrollTo(0,feedView.offsetTop);
  requestAnimationFrame(()=>{
    requestAnimationFrame(()=>{
      timelineProgrammaticScroll=false;
    });
  });
}

function showLatestTimeline(){
  renderTimelineWindow(0,{latest:true});
  if(timelineMonths.length)setTimelineSelection(timelineMonths[0].key);
  timelineProgrammaticScroll=true;
  window.scrollTo(0,feedView.offsetTop);
  requestAnimationFrame(()=>{
    requestAnimationFrame(()=>{
      timelineProgrammaticScroll=false;
    });
  });
}

function timelineOrdinal(key){
  const match=/^(\d{4})-(0[1-9]|1[0-2])$/.exec(key);
  if(!match)return null;
  return Number(match[1])*12+(Number(match[2])-1);
}

function timelineKey(ordinal){
  const year=Math.floor(ordinal/12);
  const month=(ordinal%12)+1;
  return `${String(year).padStart(4,"0")}-${String(month).padStart(2,"0")}`;
}

function resolveTimelineMonth(targetKey){
  const target=timelineOrdinal(targetKey);
  if(target===null||timelineMonths.length===0)return null;
  const exact=timelineMonths.find(entry=>entry.key===targetKey);
  if(exact)return exact;
  const later=timelineMonths
    .filter(entry=>timelineOrdinal(entry.key)>target)
    .sort((left,right)=>timelineOrdinal(left.key)-timelineOrdinal(right.key))[0];
  if(later)return later;
  return timelineMonths
    .filter(entry=>timelineOrdinal(entry.key)<target)
    .sort((left,right)=>timelineOrdinal(right.key)-timelineOrdinal(left.key))[0]||null;
}

function timelineStartForMonth(entry){
  return clampTimelineStart(entry.start-TIMELINE_MONTH_CONTEXT);
}

function jumpToTimelineMonth(targetKey){
  const entry=resolveTimelineMonth(targetKey);
  if(entry===null)return null;
  const start=timelineStartForMonth(entry);
  renderTimelineWindow(start,{
    latest:false,
    targetKey,
    focusIndex:entry.start,
  });
  const target=feed.querySelector(`[data-timeline-target="${CSS.escape(targetKey)}"]`);
  if(target){
    timelineProgrammaticScroll=true;
    target.scrollIntoView({block:"start"});
    requestAnimationFrame(()=>{
      requestAnimationFrame(()=>{
        timelineProgrammaticScroll=false;
        handleTimelineScroll();
      });
    });
  }
  setTimelineSelection(entry.key);
  return entry.key;
}

const timelineDirectory=document.querySelector("[data-timeline-directory]");
const timelineYears=document.querySelector("[data-timeline-years]");
const timelineTotal=document.querySelector("[data-timeline-total]");
let timelineSelectedKey=timelineMonths.length?timelineMonths[0].key:null;
let timelineExpandedYear=timelineMonths.length?timelineMonths[0].year:null;
let timelineScrollSyncEnabled=false;
let timelineScrollSyncScheduled=false;
let timelineProgrammaticScroll=false;

function timelineCount(entry){
  return Math.max(0,Number(entry.end)-Number(entry.start));
}

function groupedTimelineYears(){
  const groups=[];
  timelineMonths.forEach(entry=>{
    let group=groups.find(item=>item.year===entry.year);
    if(!group){
      group={year:entry.year,months:[],count:0};
      groups.push(group);
    }
    group.months.push(entry);
    group.count+=timelineCount(entry);
  });
  return groups;
}

function renderTimelineDirectory(){
  if(!timelineDirectory||!timelineYears)return;
  const groups=groupedTimelineYears();
  timelineDirectory.hidden=groups.length===0;
  if(timelineTotal)timelineTotal.textContent=`共 ${normalPosts.length} 条`;
  timelineYears.replaceChildren();
  groups.forEach(group=>{
    const section=el("section","timeline-year-group");
    const yearButton=el("button","timeline-year local-control");
    yearButton.type="button";
    yearButton.dataset.timelineYear=String(group.year);
    yearButton.setAttribute("aria-expanded",String(group.year===timelineExpandedYear));
    const yearLabel=el("span","",`${group.year} 年`);
    const yearCount=el("span","timeline-count",String(group.count));
    yearCount.dataset.timelineCount="";
    yearButton.append(yearLabel,yearCount);
    yearButton.addEventListener("click",()=>{
      timelineExpandedYear=group.year;
      renderTimelineDirectory();
    });
    section.append(yearButton);
    if(group.year===timelineExpandedYear){
      const months=el("div","timeline-months");
      group.months.forEach(entry=>{
        const monthButton=el("button","timeline-month local-control");
        monthButton.type="button";
        monthButton.dataset.timelineMonth=entry.key;
        monthButton.setAttribute("aria-current",String(entry.key===timelineSelectedKey));
        const monthLabel=el("span","",`${String(entry.month).padStart(2,"0")} 月`);
        const monthCount=el("span","timeline-count",String(timelineCount(entry)));
        monthCount.dataset.timelineCount="";
        monthButton.append(monthLabel,monthCount);
        monthButton.addEventListener("click",()=>jumpToTimelineMonth(entry.key));
        months.append(monthButton);
      });
      section.append(months);
    }
    timelineYears.append(section);
  });
}

function setTimelineSelection(key){
  const entry=timelineMonths.find(month=>month.key===key);
  if(!entry)return;
  timelineSelectedKey=entry.key;
  const yearChanged=timelineExpandedYear!==entry.year;
  if(yearChanged){
    timelineExpandedYear=entry.year;
    renderTimelineDirectory();
  }else{
    timelineYears.querySelectorAll("[data-timeline-month]").forEach(button=>{
      button.setAttribute("aria-current",String(button.dataset.timelineMonth===timelineSelectedKey));
    });
  }
}

function monthKeyForPost(bid){
  const index=normalPosts.findIndex(post=>post.bid===bid);
  return monthKeyForIndex(index);
}

function monthKeyForIndex(index){
  if(index<0)return null;
  const entry=timelineMonths.find(month=>month.start<=index && index<month.end);
  return entry?entry.key:null;
}

function handleTimelineScroll(){
  if(!timelineScrollSyncEnabled)return;
  if(timelineProgrammaticScroll)return;
  if(timelineScrollSyncScheduled)return;
  timelineScrollSyncScheduled=true;
  requestAnimationFrame(()=>{
    timelineScrollSyncScheduled=false;
    if(timelineProgrammaticScroll)return;
    const feedCards=feed.querySelectorAll("[data-bid]");
    if(!feedCards.length)return;
    const referenceTop=Math.max(72,feedView.getBoundingClientRect().top);
    const reachedDocumentEnd=window.scrollY+window.innerHeight>=
      document.documentElement.scrollHeight-1;
    let currentCard=reachedDocumentEnd?feedCards[feedCards.length-1]:null;
    if(!currentCard){
      for(const card of feedCards){
        const rect=card.getBoundingClientRect();
        if(rect.bottom>=referenceTop){
          currentCard=card;
          break;
        }
      }
    }
    if(!currentCard){
      currentCard=feedCards[feedCards.length-1];
    }
    if(currentCard){
      const key=monthKeyForPost(currentCard.dataset.bid);
      if(key)setTimelineSelection(key);
    }
  });
}

function observeFeedCards(){
  /* 滚动同步通过 scroll 事件驱动，无需在此处绑定观察者。 */
}

function initializeDesktopTimeline(){
  renderTimelineDirectory();
  window.addEventListener("scroll",handleTimelineScroll,{passive:true});
  requestAnimationFrame(()=>{
    requestAnimationFrame(()=>{
      timelineScrollSyncEnabled=true;
    });
  });
}

window.__WEISHUSHU_TIMELINE_TEST__={
  jump:jumpToTimelineMonth,
  resolve:key=>{
    const entry=resolveTimelineMonth(key);
    return entry?entry.key:null;
  },
  bounds:()=>({start:state.timelineStart,end:state.timelineEnd}),
};

const mobileTimeButton=document.querySelector('[data-action="open-time-panel"]');
const timePanel=document.querySelector("[data-time-panel]");
const timeYear=document.querySelector("[data-time-year]");
const timeMonth=document.querySelector("[data-time-month]");

function populateTimePanel(key){
  const [selectedYear,selectedMonth]=key.split("-");
  const newestYear=timelineMonths[0].year;
  const oldestYear=timelineMonths[timelineMonths.length-1].year;
  timeYear.replaceChildren();
  for(let year=newestYear;year>=oldestYear;year-=1){
    const option=el("option","",String(year));
    option.value=String(year);
    timeYear.append(option);
  }
  timeMonth.replaceChildren();
  for(let month=1;month<=12;month+=1){
    const value=String(month).padStart(2,"0");
    const option=el("option","",`${month} 月`);
    option.value=value;
    timeMonth.append(option);
  }
  timeYear.value=selectedYear;
  timeMonth.value=selectedMonth;
}

function openTimePanel(){
  if(timelineMonths.length===0)return;
  state.timelinePanelScroll=window.scrollY;
  state.timelinePanelTrigger=document.activeElement;
  timePanel.hidden=false;
  appShell.inert=true;
  populateTimePanel(state.timelineTarget||timelineMonths[0].key);
  timeYear.focus();
}

function closeTimePanel({restoreScroll=true}={}){
  timePanel.hidden=true;
  appShell.inert=false;
  if(restoreScroll)window.scrollTo(0,state.timelinePanelScroll);
  if(state.timelinePanelTrigger&&state.timelinePanelTrigger.isConnected){
    state.timelinePanelTrigger.focus();
  }
  state.timelinePanelTrigger=null;
}

function applyTimePanel(){
  const key=`${timeYear.value}-${timeMonth.value}`;
  closeTimePanel({restoreScroll:false});
  jumpToTimelineMonth(key);
}

function initializeMobileTimeline(){
  mobileTimeButton.hidden=timelineMonths.length===0;
}
