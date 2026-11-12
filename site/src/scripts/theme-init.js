(function(){var r=document.documentElement,t=null;try{t=localStorage.getItem('foldbun.theme')}catch(e){}
var v=t==='light'||t==='dark'?t:matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
r.setAttribute('data-docs','');r.setAttribute('data-theme',v)})();
