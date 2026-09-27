function r(e=new Date){return o(e,864e5)}function o(e,n){return new Date(e.getTime()+n).toISOString().replace(/\.\d{3}Z$/,"Z")}function a(e,n){let t=[];return e.forEach(i=>{t.length&&t.push(""),t.push(`# ${i}`),t.push(...n)}),t.join(`
`)}function s(e,n=new Date){return a(e,["    _state_maintenance:",`      expires: "${r(n)}"`,'      reason: "Scheduled maintenance"'])}function c(e,n=new Date){return a(e,["    _silent_mode:",'      target: "all"',`      expires: "${r(n)}"`,'      reason: "Under investigation"'])}export{r as a,o as b,s as c,c as d};
//# sourceMappingURL=chunk-AH2QVGVA.js.map
