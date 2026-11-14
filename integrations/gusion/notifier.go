// SPDX-License-Identifier: GPL-3.0-or-later
// Local notification adapter for Gusion1017/WeChat-ChatBot-Mac, pinned in THIRD_PARTY.md.
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
)

const notifierProtocol = "wechat-notifier-gusion-v1"
const notifierCommit = "9094de7859618a8124f9d3162e63c87b0c8c837a"

var notifierSession = uuid.NewString()
var notifierGroupID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}@chatroom$`)
var notifierUserID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
var notifierRequestID = regexp.MustCompile(`^[a-f0-9]{32}$`)
var notifierVersions = map[string]string{"4.1.15.55": "270135", "4.2.0.5": "270341", "4.2.0.9": "270345", "4.2.0.12": "270348"}
var notifierStatusReader = notifierCurrentStatus
var notifierDirectoryReader = refreshContactDirectory
var notifierContactsReader = notifierReadContacts
var notifierKeyStatusReader = func() (map[string]any, error) {
	result, err := callFridaExport("getDbKeyStatus")
	if err != nil {
		return nil, err
	}
	raw, err := contactRPCResultString(result)
	if err != nil {
		return nil, err
	}
	var status map[string]any
	err = json.Unmarshal([]byte(raw), &status)
	return status, err
}
var notifierEnqueue = func(message *SendMsg) error {
	select {
	case msgChan <- message:
	case <-time.After(time.Second):
		return errors.New("busy")
	}
	select {
	case err := <-message.ResultChan:
		return err
	case <-time.After(25 * time.Second):
		return errors.New("unknown")
	}
}

func registerNotifierRoutes() {
	http.HandleFunc("/notifier/status", notifierStatusHandler)
	http.HandleFunc("/notifier/groups", notifierGroupsHandler)
	http.HandleFunc("/notifier/contacts", notifierContactsHandler)
	http.HandleFunc("/notifier/submit", notifierSubmitHandler)
}

func notifierAuthorized(w http.ResponseWriter, r *http.Request, method string) bool {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != method {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return false
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") || config.OnebotToken == "" || !hmac.Equal([]byte(token), []byte(config.OnebotToken)) {
		w.WriteHeader(http.StatusUnauthorized)
		return false
	}
	return true
}

func notifierJSON(w http.ResponseWriter, code int, value any) {
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(value)
}

func notifierFailure(w http.ResponseWriter, code int) {
	notifierJSON(w, code, map[string]any{"status": "failed", "error": "native_bridge_unavailable"})
}

// Return only bounded counters and known failure codes; never include raw Frida
// exceptions, file paths, database keys, contacts, or account data in diagnostics.
func notifierDirectoryFailure(w http.ResponseWriter, err error) {
	reason := "unavailable"
	if err != nil {
		for _, code := range []string{"key_missing", "db_missing", "decrypt_failed", "unsupported"} {
			if err.Error() == "通讯录不可用："+code {
				reason = code
				break
			}
		}
	}
	diagnostics := map[string]any{}
	if state, statusErr := notifierKeyStatusReader(); statusErr == nil {
		for _, field := range []string{"installed", "passphrase", "file_api", "sqlite_api", "crypto_api"} {
			if value, ok := state[field].(bool); ok {
				diagnostics[field] = value
			}
		}
		if value, ok := state["setup_error"].(string); ok && (value == "hook_attach_failed" || value == "runtime_error") {
			diagnostics["setup_error"] = value
		}
		for _, field := range []string{"validated", "candidates"} {
			if value, ok := state[field].(float64); ok && value >= 0 && value <= 20000 && value == float64(int(value)) {
				diagnostics[field] = int(value)
			}
		}
	}
	notifierJSON(w, 503, map[string]any{"status": "failed", "error": "native_bridge_unavailable",
		"reason": reason, "directory_diagnostics": diagnostics})
}

func notifierCurrentStatus() (map[string]any, error) {
	bytes, err := os.ReadFile(config.WechatConf)
	if err != nil {
		return nil, err
	}
	var conf map[string]string
	if json.Unmarshal(bytes, &conf) != nil {
		return nil, errors.New("unsupported_build")
	}
	if build, ok := notifierVersions[conf["notifierVersion"]]; !ok || build != conf["notifierBuild"] {
		return nil, errors.New("unsupported_build")
	}
	// Check open-file metadata only; do not read any database bytes here.
	if config.WechatPid <= 0 {
		return nil, errors.New("wechat_pid_unknown")
	}
	files, err := exec.Command("/usr/sbin/lsof", "-a", "-p", strconv.Itoa(config.WechatPid), "-Fn").Output()
	if err != nil {
		return nil, errors.New("wechat_account_unknown")
	}
	accounts := make(map[string]bool)
	for _, line := range strings.Split(string(files), "\n") {
		if strings.HasPrefix(line, "n/") && strings.HasSuffix(line, "/db_storage/contact/contact.db") {
			accounts[strings.TrimSuffix(line[1:], "/db_storage/contact/contact.db")] = true
		}
	}
	if len(accounts) != 1 || !accounts[filepath.Dir(config.ImagePath)] {
		return nil, errors.New("wechat_account_changed")
	}
	result, err := callFridaExport("r3Status")
	if err != nil {
		return nil, err
	}
	raw, err := contactRPCResultString(result)
	if err != nil {
		return nil, err
	}
	var state map[string]any
	if json.Unmarshal([]byte(raw), &state) != nil {
		return nil, errors.New("invalid_status")
	}
	seeded := state["captureMode"] == "seeded"
	ready := state["active"] == true && state["resolved"] == true && seeded && myWechatId != ""
	return map[string]any{"protocol": notifierProtocol, "upstream_commit": notifierCommit,
		"version": conf["notifierVersion"], "build": conf["notifierBuild"], "session_id": notifierSession,
		"account_id": myWechatId, "ready": ready, "seeded": seeded,
		"directory_enabled": conf["contactDirectory"] == "true", "private_send_enabled": true}, nil
}

func notifierStatusHandler(w http.ResponseWriter, r *http.Request) {
	if !notifierAuthorized(w, r, http.MethodGet) {
		return
	}
	state, err := notifierStatusReader()
	if err != nil {
		notifierFailure(w, 503)
		return
	}
	notifierJSON(w, 200, map[string]any{"status": "ok", "data": state})
}

func notifierGroupsHandler(w http.ResponseWriter, r *http.Request) {
	if !notifierAuthorized(w, r, http.MethodGet) {
		return
	}
	state, err := notifierStatusReader()
	if err != nil || state["directory_enabled"] != true {
		notifierFailure(w, 503)
		return
	}
	directory, err := notifierDirectoryReader()
	if err != nil || directory == nil || !directory.OK {
		notifierDirectoryFailure(w, err)
		return
	}
	groups := make([]map[string]string, 0, len(directory.Groups))
	if len(directory.Groups) > 10000 {
		notifierFailure(w, 503)
		return
	}
	for id, entry := range directory.Groups {
		if !notifierGroupID.MatchString(id) || entry.displayName() == "" {
			continue
		}
		name := entry.Nickname
		if name == "" {
			name = entry.displayName()
		}
		group := map[string]string{"group_id": id, "group_name": name}
		if display := entry.displayName(); display != name {
			group["display_name"] = display
		}
		groups = append(groups, group)
	}
	after, err := notifierStatusReader()
	if err != nil || after["account_id"] != state["account_id"] || after["session_id"] != state["session_id"] {
		notifierFailure(w, 503)
		return
	}
	notifierJSON(w, 200, map[string]any{"status": "ok", "data": map[string]any{
		"account_id": state["account_id"], "session_id": state["session_id"], "groups": groups}})
}

func notifierValidUserID(id string) bool {
	return notifierUserID.MatchString(id) && !strings.HasPrefix(strings.ToLower(id), "gh_")
}

type notifierContact struct {
	UserID      string `json:"user_id"`
	DisplayName string `json:"display_name"`
}

func notifierReadContacts(query string, exact bool) ([]notifierContact, error) {
	if !contactDirectoryRefreshMu.TryLock() {
		return nil, errors.New("directory_busy")
	}
	defer contactDirectoryRefreshMu.Unlock()

	path := deriveDBStoragePath(config.ImagePath)
	if path == "" {
		return nil, errContactPathUnknown
	}
	result, err := callFridaExport("getContactDirectory", path, query, exact)
	if err != nil {
		return nil, err
	}
	raw, err := contactRPCResultString(result)
	if err != nil {
		return nil, err
	}
	var reply struct {
		OK       bool              `json:"ok"`
		Contacts []notifierContact `json:"contacts"`
	}
	if json.Unmarshal([]byte(raw), &reply) != nil || !reply.OK || reply.Contacts == nil {
		return nil, errors.New("contact_lookup_failed")
	}
	return reply.Contacts, nil
}

func notifierContactsHandler(w http.ResponseWriter, r *http.Request) {
	if !notifierAuthorized(w, r, http.MethodPost) {
		return
	}
	var request struct {
		Query              string `json:"query"`
		Exact              bool   `json:"exact"`
		AllowContactLookup bool   `json:"allow_contact_lookup"`
		AccountID          string `json:"account_id"`
		SessionID          string `json:"session_id"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2048))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&request) != nil || !request.AllowContactLookup ||
		strings.TrimSpace(request.Query) == "" || utf8.RuneCountInString(request.Query) > 128 ||
		strings.ContainsAny(request.Query, "\x00\r\n") ||
		(request.Exact && !notifierValidUserID(request.Query)) {
		notifierFailure(w, 400)
		return
	}
	state, err := notifierStatusReader()
	if err != nil || state["directory_enabled"] != true {
		notifierFailure(w, 503)
		return
	}
	if request.AccountID == "" || request.SessionID == "" ||
		state["account_id"] != request.AccountID || state["session_id"] != request.SessionID {
		notifierFailure(w, 409)
		return
	}
	contacts, err := notifierContactsReader(request.Query, request.Exact)
	if err != nil || contacts == nil || len(contacts) > 20 {
		notifierFailure(w, 503)
		return
	}
	seen := make(map[string]bool, len(contacts))
	for _, contact := range contacts {
		if !notifierValidUserID(contact.UserID) || seen[contact.UserID] ||
			(request.Exact && contact.UserID != request.Query) ||
			strings.TrimSpace(contact.DisplayName) == "" || utf8.RuneCountInString(contact.DisplayName) > 200 {
			notifierFailure(w, 503)
			return
		}
		seen[contact.UserID] = true
	}
	after, err := notifierStatusReader()
	if err != nil || after["account_id"] != request.AccountID || after["session_id"] != request.SessionID {
		notifierFailure(w, 409)
		return
	}
	notifierJSON(w, 200, map[string]any{"status": "ok", "data": map[string]any{
		"account_id": request.AccountID, "session_id": request.SessionID, "contacts": contacts}})
}

type notifierSubmission struct {
	RequestID string `json:"request_id"`
	GroupID   string `json:"group_id"`
	UserID    string `json:"user_id"`
	AccountID string `json:"account_id"`
	SessionID string `json:"session_id"`
	Text      string `json:"text"`
}
type notifierReceipt struct {
	hash      string
	done      chan struct{}
	submitted bool
	created   time.Time
}

var notifierReceipts = struct {
	sync.Mutex
	items map[string]*notifierReceipt
}{items: make(map[string]*notifierReceipt)}

func notifierSubmitHandler(w http.ResponseWriter, r *http.Request) {
	if !notifierAuthorized(w, r, http.MethodPost) {
		return
	}
	var request notifierSubmission
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32768))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&request) != nil {
		notifierFailure(w, 400)
		return
	}
	group := request.UserID == "" && notifierGroupID.MatchString(request.GroupID)
	private := request.GroupID == "" && notifierValidUserID(request.UserID)
	if !notifierRequestID.MatchString(request.RequestID) || (!group && !private) || strings.TrimSpace(request.Text) == "" ||
		!utf8.ValidString(request.Text) || utf8.RuneCountInString(request.Text) > 5000 {
		notifierFailure(w, 400)
		return
	}
	state, err := notifierStatusReader()
	if err != nil || state["ready"] != true || state["account_id"] != request.AccountID || state["session_id"] != request.SessionID {
		notifierFailure(w, 409)
		return
	}
	encoded, _ := json.Marshal(request)
	digest := sha256.Sum256(encoded)
	fingerprint := hex.EncodeToString(digest[:])
	notifierReceipts.Lock()
	for id, entry := range notifierReceipts.items {
		select {
		case <-entry.done:
			if time.Since(entry.created) > 24*time.Hour {
				delete(notifierReceipts.items, id)
			}
		default:
		}
	}
	entry, exists := notifierReceipts.items[request.RequestID]
	if exists && entry.hash != fingerprint {
		notifierReceipts.Unlock()
		notifierFailure(w, 409)
		return
	}
	if !exists {
		if len(notifierReceipts.items) >= 512 {
			oldest := ""
			for id, receipt := range notifierReceipts.items {
				select {
				case <-receipt.done:
					if oldest == "" || receipt.created.Before(notifierReceipts.items[oldest].created) {
						oldest = id
					}
				default:
				}
			}
			if oldest != "" {
				delete(notifierReceipts.items, oldest)
			} else {
				notifierReceipts.Unlock()
				notifierFailure(w, 429)
				return
			}
		}
		entry = &notifierReceipt{hash: fingerprint, done: make(chan struct{}), created: time.Now()}
		notifierReceipts.items[request.RequestID] = entry
	}
	notifierReceipts.Unlock()
	if !exists {
		err = notifierEnqueue(&SendMsg{GroupID: request.GroupID, UserId: request.UserID,
			Content: request.Text, Type: "text", ResultChan: make(chan error, 1)})
		entry.submitted = err == nil
		close(entry.done)
	} else {
		select {
		case <-entry.done:
		case <-time.After(27 * time.Second):
			notifierFailure(w, 503)
			return
		}
	}
	if !entry.submitted {
		notifierFailure(w, 503)
		return
	}
	notifierJSON(w, 200, map[string]any{"status": "ok", "receipt": "submitted", "request_id": request.RequestID})
}
