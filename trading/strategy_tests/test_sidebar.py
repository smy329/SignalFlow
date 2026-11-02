"""The sidebar groups appear in a fixed order."""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class SidebarOrderTests(TestCase):
    def test_groups_are_in_the_agreed_order_with_the_same_sub_pages(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))
        html = self.client.get(reverse("dashboard")).content.decode()
        nav = html[html.index("<nav"):html.index("</nav>")]
        top = [m for m in re.findall(r"(?:js-nav-label[^>]*>|block rounded-lg px-3 py-2[^>]*>)\s*([A-Za-z ]+?)\s*</", nav)]
        self.assertEqual(top, ["Dashboard", "Manual Trades", "Real Trades", "Forward Trades", "Parser Playground", "Settings"])
        manual = nav[nav.index("Manual Trades"):nav.index("Real Trades")]
        self.assertEqual(re.findall(r">(Place trade|Trades|History)<", manual), ["Place trade", "Trades", "History"])
