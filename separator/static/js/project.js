document.addEventListener('DOMContentLoaded', () => {
document.querySelectorAll('.connect-btn').forEach(btn => {
    btn.addEventListener('click', function(e){
    btn.textContent = 'Wait...';
    btn.classList.add('disabled');
    btn.setAttribute('aria-disabled', 'true');
    btn.onclick = function(){ return false; };
    });
});
});