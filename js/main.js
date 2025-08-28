document.addEventListener('DOMContentLoaded', function () {
  // すべてのドロップダウントリガーを取得
  const dropdownToggles = document.querySelectorAll('.header-nav .dropdown > a');

  dropdownToggles.forEach(toggle => {
    toggle.addEventListener('click', function (event) {
      // aタグのデフォルトの動作（ページ遷移）をキャンセル
      event.preventDefault();
      event.stopPropagation();

      // クリックされたメニューのドロップダウン要素を取得
      const targetMenu = this.nextElementSibling;

      // 他の開いているメニューをすべて閉じる
      document.querySelectorAll('.header-nav .dropdown-menu.show').forEach(menu => {
        if (menu !== targetMenu) {
          menu.classList.remove('show');
        }
      });

      // クリックされたメニューの表示をトグルする
      if (targetMenu && targetMenu.classList.contains('dropdown-menu')) {
        targetMenu.classList.toggle('show');
      }
    });
  });

  // ドキュメント全体でクリックを監視し、メニュー外がクリックされたら閉じる
  window.addEventListener('click', function (event) {
    // クリックがドロップダウン自体やその子孫でない場合
    if (!event.target.closest('.dropdown')) {
      document.querySelectorAll('.header-nav .dropdown-menu.show').forEach(menu => {
        menu.classList.remove('show');
      });
    }
  });
});