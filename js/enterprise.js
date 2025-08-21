
      document.addEventListener("DOMContentLoaded", function () {
        const parentRows = document.querySelectorAll(".parent");

        parentRows.forEach((row) => {
          row.addEventListener("click", (e) => {
            if (e.target.closest("i")) {
              if (!e.target.classList.contains("fa-caret-right")) {
                return;
              }
            }
            row.classList.toggle("open");
            let nextSibling = row.nextElementSibling;
            while (
              nextSibling &&
              (nextSibling.classList.contains("minor-item") ||
                nextSibling.classList.contains("sub-category"))
            ) {
              if (row.classList.contains("open")) {
                nextSibling.style.display = "table-row";
              } else {
                nextSibling.style.display = "none";
              }
              nextSibling = nextSibling.nextElementSibling;
            }
          });
        });
      });
    