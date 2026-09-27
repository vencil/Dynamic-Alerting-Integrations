function a(e=new Date){return new Date(e.getTime()+864e5).toISOString().replace(/\.\d{3}Z$/,"Z")}function r(e,n){let t=[];return e.forEach(i=>{t.length&&t.push(""),t.push(`# ${i}`),t.push(...n)}),t.join(`
`)}function o(e,n=new Date){return r(e,["    _state_maintenance:",`      expires: "${a(n)}"`,'      reason: "Scheduled maintenance"'])}function s(e,n=new Date){return r(e,["    _silent_mode:",'      target: "all"',`      expires: "${a(n)}"`,'      reason: "Under investigation"'])}export{a,o as b,s as c};
//# sourceMappingURL=chunk-ICBM6A6T.js.map
