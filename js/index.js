
      document.addEventListener("DOMContentLoaded", function () {
        const parentRows = document.querySelectorAll(".parent");

        parentRows.forEach((row) => {
          row.addEventListener("click", (e) => {
            if (e.target.closest("button")) {
              return;
            }
            row.classList.toggle("open");
            let nextSibling = row.nextElementSibling;
            while (
              nextSibling &&
              nextSibling.classList.contains("minor-item")
            ) {
              if (nextSibling.style.display === "table-row") {
                nextSibling.style.display = "none";
              } else {
                nextSibling.style.display = "table-row";
              }
              nextSibling = nextSibling.nextElementSibling;
            }
          });
        });

        const tabs = document.querySelectorAll(".nav-tabs li");
        const tabContents = document.querySelectorAll(".tab-content");

        tabs.forEach((tab) => {
          tab.addEventListener("click", () => {
            tabs.forEach(item => item.classList.remove("active"));
            tab.classList.add("active");

            const target = tab.getAttribute("data-tab");
            tabContents.forEach(content => {
              if (content.id === target) {
                content.classList.add("active");
              } else {
                content.classList.remove("active");
              }
            });

            if (target === 'profit') {
                calculateProfit();
            }
          });
        });

        function calculateProfit() {
            const softwareTable = document.getElementById('software-table').tBodies[0];
            const adjustmentTable = document.getElementById('adjustment-table').tBodies[0];
            const profitTable = document.getElementById('profit-table').tBodies[0];

            for (let i = 0; i < softwareTable.rows.length; i++) {
                for (let j = 1; j < softwareTable.rows[i].cells.length; j++) {
                    const softwareCell = softwareTable.rows[i].cells[j];
                    const adjustmentCell = adjustmentTable.rows[i].cells[j];
                    const profitCell = profitTable.rows[i].cells[j];

                    if (!softwareCell || !adjustmentCell || !profitCell) continue;

                    const softwareValue = parseFloat(softwareCell.innerText.replace(/,/g, '')) || 0;
                    const adjustmentValue = parseFloat(adjustmentCell.innerText.replace(/,/g, '')) || 0;

                    const sum = softwareValue + adjustmentValue;
                    if(profitCell) {
                        profitCell.innerText = sum.toLocaleString();
                    }
                }
            }
        }
      });
    