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

document.querySelectorAll('.subpages-block-container').forEach(container => {
    const list = container.querySelector('.subpages-block');
    const buttons = container.querySelectorAll('.subpages-block__sort');
    let activeField = null;
    let direction = 'desc';

    Array.from(list.children).forEach((item, index) => {
        item.dataset.originalOrder = index;
    });

    const buildTree = () => {
        const roots = [];
        const stack = [];

        Array.from(list.children).forEach(item => {
            const level = Number(item.dataset.level);
            const node = {item, children: []};

            stack.length = level;
            if (level > 0 && stack[level - 1]) {
                stack[level - 1].children.push(node);
            } else {
                roots.push(node);
            }
            stack[level] = node;
        });

        return roots;
    };

    const sortNodes = (nodes, field) => {
        nodes.sort((left, right) => {
            const leftDate = left.item.dataset[field] || '';
            const rightDate = right.item.dataset[field] || '';
            const dateOrder = leftDate.localeCompare(rightDate);

            if (dateOrder !== 0) {
                return direction === 'asc' ? dateOrder : -dateOrder;
            }
            return Number(left.item.dataset.originalOrder) - Number(right.item.dataset.originalOrder);
        });
        nodes.forEach(node => sortNodes(node.children, field));
    };

    const appendNodes = (nodes, fragment) => {
        nodes.forEach(node => {
            fragment.appendChild(node.item);
            appendNodes(node.children, fragment);
        });
    };

    buttons.forEach(button => {
        button.addEventListener('click', () => {
            const field = button.dataset.sortField;
            direction = activeField === field && direction === 'desc' ? 'asc' : 'desc';
            activeField = field;

            const tree = buildTree();
            sortNodes(tree, field);
            const fragment = document.createDocumentFragment();
            appendNodes(tree, fragment);
            list.appendChild(fragment);

            buttons.forEach(currentButton => {
                const indicator = currentButton.querySelector('.subpages-block__sort-indicator');
                indicator.textContent = currentButton === button
                    ? (direction === 'asc' ? '↑' : '↓')
                    : '↕';
            });
        });
    });
});
});
