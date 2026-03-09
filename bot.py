import os
import logging
import asyncio
import random
import string
import psutil
import re
from datetime import datetime, timedelta
from typing import Union, Optional
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated, ChatPermissions,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, InputFile
)
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from contextlib import suppress

load_dotenv()

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
MONGO_URI = os.environ.get("MONGO_URI", "")
DB_NAME = os.environ.get("DB_NAME", "group_management_bot")

# Settings
LEVEL_MULTIPLIER = 100  # 100 messages = Level 1
MAX_WARNS = 4
MUTE_DURATION = 3600  # 1 hour
RAM_LIMIT = 95  # 95%
CPU_LIMIT = 95
CHECK_INTERVAL = 300  # 5 minutes

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== DATABASE ====================
class Database:
    def __init__(self):
        self.client = None
        self.db = None
    
    async def connect(self):
        self.client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.client[DB_NAME]
        
        # Create indexes
        await self.db.users.create_index("user_id", unique=True)
        await self.db.groups.create_index("group_id", unique=True)
        await self.db.captcha.create_index("user_id")
        await self.db.warns.create_index([("user_id", 1), ("group_id", 1)])
        print("✅ Database connected!")
    
    # User operations
    async def get_user(self, user_id: int):
        return await self.db.users.find_one({"user_id": user_id})
    
    async def update_user(self, user_id: int, data: dict):
        await self.db.users.update_one(
            {"user_id": user_id},
            {"$set": data},
            upsert=True
        )
    
    async def add_message_count(self, user_id: int, group_id: int):
        user = await self.get_user(user_id) or {
            "user_id": user_id,
            "groups": {},
            "level": 0,
            "total_messages": 0,
            "active": False
        }
        
        if str(group_id) not in user.get("groups", {}):
            user["groups"][str(group_id)] = 0
        
        user["groups"][str(group_id)] += 1
        user["total_messages"] += 1
        
        old_level = user.get("level", 0)
        new_level = user["total_messages"] // LEVEL_MULTIPLIER
        
        await self.update_user(user_id, user)
        
        return new_level > old_level, new_level
    
    # Group operations
    async def get_group(self, group_id: int):
        return await self.db.groups.find_one({"group_id": group_id})
    
    async def update_group(self, group_id: int, data: dict):
        await self.db.groups.update_one(
            {"group_id": group_id},
            {"$set": data},
            upsert=True
        )
    
    async def get_all_groups(self):
        cursor = self.db.groups.find({})
        async for group in cursor:
            yield group
    
    async def get_all_users(self):
        cursor = self.db.users.find({"active": True})
        async for user in cursor:
            yield user
    
    # Captcha operations
    async def save_captcha(self, user_id: int, group_id: int, code: str):
        await self.db.captcha.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "group_id": group_id,
                    "code": code,
                    "created_at": datetime.utcnow()
                }
            },
            upsert=True
        )
    
    async def verify_captcha(self, user_id: int, code: str):
        captcha = await self.db.captcha.find_one({"user_id": user_id})
        if captcha and captcha["code"] == code:
            await self.db.captcha.delete_one({"user_id": user_id})
            return captcha["group_id"]
        return None
    
    # Warn operations
    async def add_warn(self, user_id: int, group_id: int, reason: str, admin_id: int):
        warn = {
            "user_id": user_id,
            "group_id": group_id,
            "reason": reason,
            "admin_id": admin_id,
            "date": datetime.utcnow()
        }
        
        await self.db.warns.insert_one(warn)
        
        # Count warns
        count = await self.db.warns.count_documents({
            "user_id": user_id,
            "group_id": group_id
        })
        
        return count
    
    async def get_warns(self, user_id: int, group_id: int):
        cursor = self.db.warns.find({
            "user_id": user_id,
            "group_id": group_id
        }).sort("date", -1)
        
        warns = []
        async for warn in cursor:
            warns.append(warn)
        
        return warns
    
    async def clear_warns(self, user_id: int, group_id: int):
        await self.db.warns.delete_many({
            "user_id": user_id,
            "group_id": group_id
        })

db = Database()

# ==================== INIT BOT ====================
bot = Bot(
    token=BOT_TOKEN,
    parse_mode=ParseMode.HTML
)
dp = Dispatcher()

# ==================== KEYBOARDS ====================
class Keyboards:
    @staticmethod
    def main_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 My Groups", callback_data="my_groups")],
            [InlineKeyboardButton(text="➕ Add Groups", callback_data="add_group")],
            [InlineKeyboardButton(text="📊 Stats", callback_data="bot_stats")],
            [InlineKeyboardButton(text="⚙️ Settings", callback_data="main_settings")],
            [InlineKeyboardButton(text="📢 Broadcast", callback_data="broadcast_menu")],
            [InlineKeyboardButton(text="🔄 Check Resources", callback_data="check_resources")]
        ])
    
    @staticmethod
    def group_menu(group_id: int):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Members", callback_data=f"members:{group_id}")],
            [
                InlineKeyboardButton(text="🔇 Mute", callback_data=f"gmute:{group_id}"),
                InlineKeyboardButton(text="🔊 Unmute", callback_data=f"gunmute:{group_id}")
            ],
            [
                InlineKeyboardButton(text="⛔ Ban", callback_data=f"gban:{group_id}"),
                InlineKeyboardButton(text="✅ Unban", callback_data=f"gunban:{group_id}")
            ],
            [InlineKeyboardButton(text="⚠️ Warns", callback_data=f"gwarns:{group_id}")],
            [InlineKeyboardButton(text="🚫 Anti-Forward", callback_data=f"antifwd:{group_id}")],
            [InlineKeyboardButton(text="📝 Welcome", callback_data=f"welcome:{group_id}")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ])
    
    @staticmethod
    def captcha_keyboard(user_id: int):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🤖 I'm Not Robot", callback_data=f"captcha:{user_id}")
        ]])
    
    @staticmethod
    def verify_button(group_id: int):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Start Chatting", callback_data=f"join_group:{group_id}")
        ]])
    
    @staticmethod
    def unmute_button(user_id: int):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔊 Unmute", callback_data=f"unmute:{user_id}")
        ]])
    
    @staticmethod
    def broadcast_menu():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 To Users", callback_data="broadcast_users")],
            [InlineKeyboardButton(text="👥 To Groups", callback_data="broadcast_groups")],
            [InlineKeyboardButton(text="👥 To All", callback_data="broadcast_all")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ])
    
    @staticmethod
    def welcome_settings(group_id: int):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Set Text", callback_data=f"set_welcome_text:{group_id}")],
            [InlineKeyboardButton(text="🖼 Set Photo", callback_data=f"set_welcome_photo:{group_id}")],
            [InlineKeyboardButton(text="❌ Remove Photo", callback_data=f"del_welcome_photo:{group_id}")],
            [InlineKeyboardButton(text="🔙 Back", callback_data=f"group_menu:{group_id}")]
        ])

# ==================== UTILITIES ====================
class Utils:
    @staticmethod
    def generate_captcha(length: int = 6):
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    
    @staticmethod
    def parse_time(time_str: str) -> int:
        """Convert time string to seconds"""
        time_str = time_str.lower()
        if time_str.endswith('h'):
            return int(time_str[:-1]) * 3600
        elif time_str.endswith('m'):
            return int(time_str[:-1]) * 60
        elif time_str.endswith('d'):
            return int(time_str[:-1]) * 86400
        else:
            return int(time_str) * 60  # Default minutes
    
    @staticmethod
    async def is_admin(message: Message):
        if message.from_user.id == OWNER_ID:
            return True
        
        try:
            member = await message.bot.get_chat_member(
                message.chat.id, 
                message.from_user.id
            )
            return member.status in ["administrator", "creator"]
        except:
            return False
    
    @staticmethod
    async def is_owner(message: Message):
        return message.from_user.id == OWNER_ID
    
    @staticmethod
    async def check_resources():
        cpu = psutil.cpu_percent()
        memory = psutil.virtual_memory().percent
        
        status = "✅ Normal"
        if cpu > CPU_LIMIT or memory > RAM_LIMIT:
            status = "⚠️ CRITICAL"
        
        return {
            "cpu": cpu,
            "memory": memory,
            "status": status
        }
    
    @staticmethod
    async def send_log(bot, action: str, text: str):
        """Send log to owner"""
        await bot.send_message(
            OWNER_ID,
            f"📝 <b>Log: {action}</b>\n\n{text}"
        )
    
    @staticmethod
    def format_welcome_text(text: str, user: types.User, group, stats: dict = None):
        replacements = {
            "{mention}": user.mention_html(),
            "{name}": user.full_name,
            "{id}": str(user.id),
            "{username}": f"@{user.username}" if user.username else "No username",
            "{date}": datetime.now().strftime("%Y-%m-%d"),
            "{time}": datetime.now().strftime("%H:%M:%S"),
            "{group_name}": group.get("title", "Group"),
            "{member_count}": str(stats.get("member_count", 0)) if stats else "0"
        }
        
        for key, value in replacements.items():
            text = text.replace(key, value)
        
        return text

utils = Utils()

# ==================== STATES ====================
class AddGroupState(StatesGroup):
    waiting_for_group_id = State()

# ==================== HANDLERS ====================

# Start command - Main Menu
@dp.message(Command("start"))
async def start_command(message: Message):
    user_id = message.from_user.id
    
    # Save user
    await db.update_user(user_id, {
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "last_seen": datetime.utcnow()
    })
    
    # Main menu
    text = (
        f"👋 <b>Welcome {message.from_user.full_name}!</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📊 Status: Owner\n\n"
        f"Use the buttons below to manage your groups."
    )
    
    await message.reply(text, reply_markup=Keyboards.main_menu())

# ==================== CALLBACK HANDLERS ====================

@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 <b>Main Menu</b>\n\nChoose an option:",
        reply_markup=Keyboards.main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "add_group")
async def add_group_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📝 <b>Add Group</b>\n\n"
        "Please send me the Group ID of the group you want to add.\n\n"
        "ℹ️ You must be an admin in that group.\n"
        "You can find the group ID by:\n"
        "1. Forwarding a message from the group to @userinfobot\n"
        "2. Or asking in the group's description",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Cancel", callback_data="main_menu")
        ]])
    )
    await state.set_state(AddGroupState.waiting_for_group_id)
    await callback.answer()

@dp.message(AddGroupState.waiting_for_group_id)
async def process_group_id(message: Message, state: FSMContext):
    try:
        group_id = int(message.text)
    except ValueError:
        await message.reply("❌ Invalid group ID. Please send a valid number.")
        return
    
    try:
        member = await bot.get_chat_member(group_id, message.from_user.id)
        is_admin = member.status in ["administrator", "creator"]
        
        if not is_admin and message.from_user.id != OWNER_ID:
            await message.reply(
                f"❌ <b>Access Denied</b>\n\n"
                f"You must be an admin in that group to add it.\n"
                f"Group ID: <code>{group_id}</code>"
            )
            await state.clear()
            return
        
        group_info = await bot.get_chat(group_id)
        group_title = group_info.title or f"Group {group_id}"
        member_count = await bot.get_chat_member_count(group_id)
        
        await db.update_group(group_id, {
            "title": group_title,
            "member_count": member_count,
            "owner_id": message.from_user.id,
            "added_at": datetime.utcnow(),
            "anti_forward": False
        })
        
        await message.reply(
            f"✅ <b>Group Added Successfully!</b>\n\n"
            f"📁 Group: {group_title}\n"
            f"🆔 ID: <code>{group_id}</code>\n"
            f"👥 Members: {member_count}\n\n"
            f"The group is now connected to the bot.",
            reply_markup=Keyboards.main_menu()
        )
        
        await utils.send_log(
            bot,
            "GROUP_ADDED",
            f"User {message.from_user.mention_html()} added group: {group_title} ({group_id})"
        )
        
    except Exception as e:
        error_msg = str(e)
        await message.reply(
            f"❌ <b>Error</b>\n\n"
            f"Failed to add group: {error_msg}\n\n"
            f"Make sure:\n"
            f"• The group ID is correct\n"
            f"• The bot is in the group\n"
            f"• The bot is an admin in the group\n"
            f"• You are an admin in the group"
        )
    
    await state.clear()

@dp.callback_query(F.data == "my_groups")
async def my_groups_callback(callback: CallbackQuery):
    groups = []
    async for group in db.get_all_groups():
        groups.append(group)
    
    if not groups:
        await callback.message.edit_text(
            "❌ No groups connected yet.\n\n"
            "Add me to a group and make me admin first!",
            reply_markup=Keyboards.main_menu()
        )
        await callback.answer()
        return
    
    text = "📋 <b>Your Groups:</b>\n\n"
    keyboard = []
    
    for group in groups[:10]:  # Show first 10
        group_id = group["group_id"]
        title = group.get("title", "Unknown")
        text += f"• {title}\n"
        keyboard.append([
            InlineKeyboardButton(text=f"📁 {title}", callback_data=f"group_menu:{group_id}")
        ])
    
    keyboard.append([InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("group_menu:"))
async def group_menu_callback(callback: CallbackQuery):
    group_id = int(callback.data.split(":")[1])
    group = await db.get_group(group_id)
    
    if not group:
        await callback.answer("Group not found!")
        return
    
    text = (
        f"📁 <b>Group: {group.get('title', 'Unknown')}</b>\n\n"
        f"🆔 ID: <code>{group_id}</code>\n"
        f"📊 Members: {group.get('member_count', 0)}\n"
        f"🚫 Anti-Forward: {'✅ ON' if group.get('anti_forward', False) else '❌ OFF'}\n\n"
        f"Choose an action:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.group_menu(group_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("antifwd:"))
async def toggle_antiforward(callback: CallbackQuery):
    group_id = int(callback.data.split(":")[1])
    group = await db.get_group(group_id)
    
    current = group.get("anti_forward", False)
    await db.update_group(group_id, {"anti_forward": not current})
    
    status = "✅ ENABLED" if not current else "❌ DISABLED"
    
    await callback.answer(f"Anti-Forward {status}!")
    await group_menu_callback(callback)

@dp.callback_query(F.data == "bot_stats")
async def bot_stats_callback(callback: CallbackQuery):
    # Get stats
    users_count = await db.db.users.count_documents({})
    groups_count = await db.db.groups.count_documents({})
    
    # Get resources
    resources = await utils.check_resources()
    
    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Users: {users_count}\n"
        f"👥 Groups: {groups_count}\n\n"
        f"💻 <b>Resources:</b>\n"
        f"CPU: {resources['cpu']}%\n"
        f"Memory: {resources['memory']}%\n"
        f"Status: {resources['status']}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "check_resources")
async def check_resources_callback(callback: CallbackQuery):
    resources = await utils.check_resources()
    
    text = (
        f"💻 <b>System Resources</b>\n\n"
        f"CPU: {resources['cpu']}%\n"
        f"Memory: {resources['memory']}%\n"
        f"Status: {resources['status']}\n\n"
        f"Limit: CPU {CPU_LIMIT}% | RAM {RAM_LIMIT}%"
    )
    
    # Alert if high
    if resources['cpu'] > CPU_LIMIT or resources['memory'] > RAM_LIMIT:
        text += "\n\n⚠️ <b>WARNING: Resources are high!</b>"
        await utils.send_log(
            callback.bot,
            "HIGH RESOURCES",
            f"CPU: {resources['cpu']}%\nRAM: {resources['memory']}%"
        )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "broadcast_menu")
async def broadcast_menu_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        "📢 <b>Broadcast Menu</b>\n\n"
        "Choose who to broadcast to:",
        reply_markup=Keyboards.broadcast_menu()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("broadcast_"))
async def broadcast_type_callback(callback: CallbackQuery):
    b_type = callback.data.split("_")[1]
    
    # Store broadcast type in memory
    callback.bot.broadcast_type = b_type
    callback.bot.broadcast_message = None
    
    await callback.message.edit_text(
        f"📢 <b>Send the message to broadcast</b>\n\n"
        f"Target: <b>{b_type.upper()}</b>\n\n"
        f"Reply to this message with the content to broadcast.\n"
        f"Forward a message or send text/photo/video.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Cancel", callback_data="broadcast_menu")
        ]])
    )
    await callback.answer()

# ==================== CAPTCHA HANDLERS ====================

@dp.chat_member()
async def handle_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        user = event.new_chat_member.user
        group_id = event.chat.id
        
        # Restrict user
        try:
            await bot.restrict_chat_member(
                group_id,
                user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
        except:
            pass
        
        # Send verification
        await bot.send_message(
            group_id,
            f"👋 Welcome {user.mention_html()}!\n\n"
            f"🔐 Please verify you're not a robot.\n"
            f"Click the button below to get CAPTCHA.",
            reply_markup=Keyboards.captcha_keyboard(user.id)
        )

@dp.callback_query(F.data.startswith("captcha:"))
async def request_captcha(callback: CallbackQuery):
    user_id = int(callback.data.split(":")[1])
    
    if callback.from_user.id != user_id:
        await callback.answer("This is not for you!", show_alert=True)
        return
    
    # Generate CAPTCHA
    captcha_code = utils.generate_captcha()
    await db.save_captcha(user_id, callback.message.chat.id, captcha_code)
    
    await callback.message.edit_text(
        f"🔐 <b>CAPTCHA Verification</b>\n\n"
        f"Please enter this code:\n\n"
        f"<code>{captcha_code}</code>\n\n"
        f"Reply to this message with the code.",
        reply_markup=None
    )
    await callback.answer()

@dp.message(F.reply_to_message)
async def verify_captcha_code(message: Message):
    if not message.reply_to_message.text or "CAPTCHA" not in message.reply_to_message.text:
        return
    
    code = message.text.strip()
    group_id = await db.verify_captcha(message.from_user.id, code)
    
    if group_id:
        # Unrestrict user
        try:
            await bot.restrict_chat_member(
                group_id,
                message.from_user.id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            )
        except:
            pass
        
        # Update user active status
        await db.update_user(message.from_user.id, {
            "active": True,
            "activated_at": datetime.utcnow()
        })
        
        # Get group for welcome
        group = await db.get_group(group_id)
        
        # Send welcome
        welcome_text = group.get("welcome_text", "🎉 Welcome {mention} to the group!")
        welcome_text = utils.format_welcome_text(
            welcome_text,
            message.from_user,
            group
        )
        
        welcome_photo = group.get("welcome_photo")
        
        if welcome_photo:
            try:
                await message.reply_photo(
                    photo=welcome_photo,
                    caption=welcome_text,
                    reply_markup=Keyboards.verify_button(group_id)
                )
            except:
                await message.reply_text(
                    welcome_text,
                    reply_markup=Keyboards.verify_button(group_id)
                )
        else:
            await message.reply_text(
                welcome_text,
                reply_markup=Keyboards.verify_button(group_id)
            )
        
        # Send to group
        await bot.send_message(
            group_id,
            f"🎉 {message.from_user.mention_html()} has joined the group!",
            reply_markup=Keyboards.verify_button(group_id)
        )
    else:
        await message.reply("❌ Invalid CAPTCHA code. Please try again.")

@dp.callback_query(F.data.startswith("join_group:"))
async def join_group_callback(callback: CallbackQuery):
    group_id = int(callback.data.split(":")[1])
    
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("✅ You can now chat in the group!")
    except:
        pass
    
    await callback.answer("Welcome to the group!")

# ==================== MESSAGE COUNT & LEVEL ====================

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def count_messages(message: Message):
    # Skip commands
    if message.text and message.text.startswith("/"):
        return
    
    # Skip non-text messages
    if not message.text:
        return
    
    # Check if user is active
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("active", False):
        return
    
    # Count message
    leveled_up, new_level = await db.add_message_count(
        message.from_user.id,
        message.chat.id
    )
    
    if leveled_up:
        await message.reply(
            f"🎉 <b>Level Up!</b>\n\n"
            f"{message.from_user.mention_html()} has reached "
            f"<b>Level {new_level}</b>!\n\n"
            f"Total messages: {user.get('total_messages', 0)}"
        )

# ==================== ADMIN COMMANDS ====================

@dp.message(Command("mute"))
async def mute_command(message: Message, command: CommandObject):
    if not await utils.is_admin(message):
        return
    
    if not message.reply_to_message:
        await message.reply("Please reply to a user's message to mute them.")
        return
    
    args = command.args
    duration = 3600  # Default 1 hour
    reason = "No reason"
    
    if args:
        parts = args.split(maxsplit=1)
        if parts:
            duration = utils.parse_time(parts[0])
        if len(parts) > 1:
            reason = parts[1]
    
    user_id = message.reply_to_message.from_user.id
    
    try:
        until_date = datetime.now() + timedelta(seconds=duration)
        await bot.restrict_chat_member(
            message.chat.id,
            user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        
        text = (
            f"🔇 <b>User Muted</b>\n\n"
            f"User: {message.reply_to_message.from_user.mention_html()}\n"
            f"ID: <code>{user_id}</code>\n"
            f"Duration: {duration // 60} minutes\n"
            f"Reason: {reason}"
        )
        
        await message.reply(text, reply_markup=Keyboards.unmute_button(user_id))
        
        # Log
        await utils.send_log(bot, "MUTE", text)
        
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

@dp.message(Command("unmute"))
async def unmute_command(message: Message):
    if not await utils.is_admin(message):
        return
    
    if not message.reply_to_message:
        await message.reply("Please reply to a user's message to unmute them.")
        return
    
    user_id = message.reply_to_message.from_user.id
    
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        
        text = (
            f"🔊 <b>User Unmuted</b>\n\n"
            f"User: {message.reply_to_message.from_user.mention_html()}\n"
            f"ID: <code>{user_id}</code>"
        )
        
        await message.reply(text)
        await utils.send_log(bot, "UNMUTE", text)
        
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

@dp.message(Command("ban"))
async def ban_command(message: Message):
    if not await utils.is_admin(message):
        return
    
    if not message.reply_to_message:
        await message.reply("Please reply to a user's message to ban them.")
        return
    
    user_id = message.reply_to_message.from_user.id
    reason = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else "No reason"
    
    try:
        await bot.ban_chat_member(message.chat.id, user_id)
        
        text = (
            f"⛔ <b>User Banned</b>\n\n"
            f"User: {message.reply_to_message.from_user.mention_html()}\n"
            f"ID: <code>{user_id}</code>\n"
            f"Reason: {reason}"
        )
        
        await message.reply(text)
        await utils.send_log(bot, "BAN", text)
        
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

@dp.message(Command("unban"))
async def unban_command(message: Message):
    if not await utils.is_admin(message):
        return
    
    if not message.reply_to_message:
        await message.reply("Please reply to a user's message to unban them.")
        return
    
    user_id = message.reply_to_message.from_user.id
    
    try:
        await bot.unban_chat_member(message.chat.id, user_id)
        
        text = (
            f"✅ <b>User Unbanned</b>\n\n"
            f"User: {message.reply_to_message.from_user.mention_html()}\n"
            f"ID: <code>{user_id}</code>"
        )
        
        await message.reply(text)
        await utils.send_log(bot, "UNBAN", text)
        
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

@dp.callback_query(F.data.startswith("unmute:"))
async def unmute_callback(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Only owner can unmute!", show_alert=True)
        return
    
    user_id = int(callback.data.split(":")[1])
    
    try:
        await bot.restrict_chat_member(
            callback.message.chat.id,
            user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"✅ User <code>{user_id}</code> unmuted.")
        await callback.answer("User unmuted!")
        
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)

# ==================== ANTI-FORWARDING ====================

@dp.message(F.forward_date)
async def handle_forwarding(message: Message):
    group = await db.get_group(message.chat.id)
    
    if group and group.get("anti_forward", False):
        # Delete forwarded message
        await message.delete()
        
        # Add warn
        warns = await db.add_warn(
            message.from_user.id,
            message.chat.id,
            "Forwarding messages",
            bot.id
        )
        
        warn_text = (
            f"⚠️ <b>Warning {warns}/{MAX_WARNS}</b>\n\n"
            f"User: {message.from_user.mention_html()}\n"
            f"ID: <code>{message.from_user.id}</code>\n"
            f"Reason: Forwarding messages"
        )
        
        warn_msg = await message.reply(warn_text)
        
        # Auto mute if max warns
        if warns >= MAX_WARNS:
            until_date = datetime.now() + timedelta(seconds=MUTE_DURATION)
            try:
                await bot.restrict_chat_member(
                    message.chat.id,
                    message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await message.reply(
                    f"🔇 {message.from_user.mention_html()} has been auto-muted for "
                    f"{MUTE_DURATION // 60} minutes due to exceeding warn limit."
                )
            except Exception:
                pass

# ==================== MAIN ====================

async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Please add it to your environment secrets.")
        return
    if not MONGO_URI:
        logger.error("MONGO_URI is not set. Please add it to your environment secrets.")
        return

    await db.connect()
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
