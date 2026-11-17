document.addEventListener('DOMContentLoaded', () => {
  const slider = document.getElementById('denoise-time');
  const bars = document.getElementById('weight-bars');
  const errors = [2.8, 2.1, 1.4, 0.7, 0.15, 0.5, 1.2, 2.0];
  errors.forEach((e,i) => {
    const bar = document.createElement('div'); bar.className = 'weight-bar' + (i===4 ? ' target' : '');
    const value = document.createElement('b'); const fill = document.createElement('i');
    const label = document.createElement('span'); label.textContent = 'h' + (i+1);
    bar.append(value,fill,label); bars.append(bar);
  });
  function updateWeights() {
    const t = Number(slider.value) / 100, beta = t*t / ((1-t)*(1-t));
    const weights = errors.map(e => Math.exp(-beta*(e-Math.min(...errors))));
    const total = weights.reduce((a,b)=>a+b,0);
    Array.from(bars.children).forEach((bar,i)=>{
      const weight = weights[i]/total;
      bar.style.setProperty('--weight', (weight*115)+'px');
      bar.querySelector('b').textContent = (weight*100).toFixed(0)+'%';
    });
    document.getElementById('denoise-value').textContent = slider.value+'%';
    bars.setAttribute('aria-label', 'Illustrative temporal weights. Target step h5 has '+(weights[4]/total*100).toFixed(0)+' percent weight at '+slider.value+' percent denoising progress.');
  }
  slider.addEventListener('input',updateWeights); updateWeights();
  document.getElementById('copy-citation').addEventListener('click', async () => {
    const text = document.getElementById('bibtex').textContent;
    try { await navigator.clipboard.writeText(text); document.getElementById('copy-status').textContent='Citation copied.'; }
    catch { document.getElementById('copy-status').textContent='Select and copy the citation above.'; }
  });
});