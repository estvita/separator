document.addEventListener('DOMContentLoaded', () => {
document.querySelectorAll('table').forEach(table => {
    if (table.closest('.table-responsive, .table-scroll')) {
    return;
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll';
    table.parentNode.insertBefore(wrapper, table);
    wrapper.appendChild(table);
});

document.querySelectorAll('.connect-btn').forEach(btn => {
    btn.addEventListener('click', function(e){
    btn.textContent = 'Wait...';
    btn.classList.add('disabled');
    btn.setAttribute('aria-disabled', 'true');
    btn.onclick = function(){ return false; };
    });
});
});
