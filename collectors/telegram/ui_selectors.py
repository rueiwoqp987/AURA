
CHATS = f'//android.widget.TextView[@text="Chats"]'
CONTACTS = f'//android.widget.TextView[@text="Contacts"]'
SETTINGS = f'//android.widget.TextView[@text="Settings"]'
PROFILE = f'//android.widget.TextView[@text="Profile"]'


CHAT_LIST_SEARCH_TEXT = "Search Chats"
CHAT_LIST_SEARCH_XPATH = f'//android.widget.EditText[@content-desc="{CHAT_LIST_SEARCH_TEXT}" or @text="{CHAT_LIST_SEARCH_TEXT}"]'
CHAT_LIST_TOP_TEXTS = ["Telegram", "Waiting for network...", "Waiting for network", "Connecting..."]
CHAT_LIST_HEADER_ACTION_DESCS = ["Search"]


CONTACT_LIST_SEARCH_TEXT = "Search Contacts"
CONTACT_LIST_SEARCH_XPATH = f'//android.widget.EditText[@content-desc="{CONTACT_LIST_SEARCH_TEXT}" or @text="{CONTACT_LIST_SEARCH_TEXT}"]'
CONTACT_LIST_HEADER_ACTION_DESCS = ["Search Contacts", "Change sorting"]


PROFILE_USERNAME_PREFIX = "Username:"
PROFILE_MOBILE_PREFIX = "Mobile:"
PROFILE_USERNAME_LABEL = "Username"
PROFILE_MOBILE_LABEL = "Mobile"
PROFILE_ACTION_TEXTS = ["Set Photo", "Edit Info", "Settings"]
PROFILE_HEADER_DESCS = ["QR Code", "More options"]


CHATRROM_END_ANCHOR = ['Your contacts on Telegram', 'Tap on the button to start a new chat']
CHATROOM_ELEM = f'//androidx.recyclerview.widget.RecyclerView//android.view.ViewGroup'
CHATROOM_HEADER_BACK_DESC = "Go back"
CHATROOM_HEADER_ACTION_DESCS = ["Call", "More options"]
CHATROOM_COMPOSER_DESC = "Web tabs "
CHATROOM_COMPOSER_BOX = f'//android.widget.FrameLayout[@content-desc="{CHATROOM_COMPOSER_DESC}"]'
CHATROOM_TYPE_PAGE = f'//android.widget.FrameLayout[@clickable="true" and @focusable="true" and @content-desc!=""]'
CHATROOM_PROFILE_SKIP_STRING = ["Username", "Waiting for network...", "Message", "Mute", "Share", "Stop", "Open App", "Video Chat", "Leave", "Links", "Files", "Polls", "Bio", "Subscribers", "Administrators", "Channel Settings", "Add Story"]
CHATROOM_PROFILE_TYPE = ["monthly user", "bot", "subscriber", "private channel", "public channel", "member", "private group", "public group", "service notifications", "last seen", "online"]
CHATROOM_GROUP_MEMBER_INFO = f'//androidx.recyclerview.widget.RecyclerView/android.widget.FrameLayout'


ACCOUNTS_SECTION_TITLE = "Accounts"
GO_TO_BOTTOM_DESC = "Go to bottom"
BANNER_CLOSE_XPATH = f'//android.widget.FrameLayout[@clickable="true" and .//android.widget.TextView[@text!=""] and .//android.widget.ImageView[@clickable="true"]] //android.widget.ImageView[@clickable="true"]'
